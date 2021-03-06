# -*- coding: utf-8 -*-
import copy
import itertools
import operator
from collections import defaultdict
from functools import partial

import numpy as np

from pandas._libs import internals as libinternals

from pandas.core.base import PandasObject

from pandas.core.dtypes.dtypes import (
    ExtensionDtype,
    PandasExtensionDtype)
from pandas.core.dtypes.common import (
    _NS_DTYPE,
    ensure_int64,
    is_timedelta64_dtype,
    is_datetime64_dtype, is_datetimetz,
    is_categorical_dtype,
    is_datetimelike_v_numeric,
    is_float_dtype, is_numeric_dtype,
    is_numeric_v_string_like, is_extension_type,
    is_extension_array_dtype,
    is_scalar,
    _get_dtype)
from pandas.core.dtypes.cast import (
    maybe_promote,
    infer_dtype_from_scalar,
    find_common_type)
from pandas.core.dtypes.missing import isna
import pandas.core.dtypes.concat as _concat
from pandas.core.dtypes.generic import ABCSeries, ABCExtensionArray

import pandas.core.algorithms as algos

from pandas.core.index import Index, MultiIndex, ensure_index
from pandas.core.indexing import maybe_convert_indices
from pandas.io.formats.printing import pprint_thing

from pandas.core.sparse.array import _maybe_to_sparse
from pandas._libs import lib, tslibs
from pandas._libs.internals import BlockPlacement

from pandas.util._decorators import cache_readonly
from pandas.util._validators import validate_bool_kwarg
from pandas.compat import range, map, zip, u

from .blocks import (
    Block,
    _extend_blocks, _merge_blocks, _safe_reshape,
    make_block, get_block_type)
from .blocks import (  # noqa:F401
    _block2d_to_blocknd, _factor_indexer, _block_shape,  # io.pytables
    FloatBlock, IntBlock, ComplexBlock, BoolBlock, ObjectBlock,
    TimeDeltaBlock, DatetimeBlock, DatetimeTZBlock,
    CategoricalBlock, ExtensionBlock, SparseBlock, ScalarBlock)

# TODO: flexible with index=None and/or items=None


class BlockManager(PandasObject):
    """
    Core internal data structure to implement DataFrame, Series, Panel, etc.

    Manage a bunch of labeled 2D mixed-type ndarrays. Essentially it's a
    lightweight blocked set of labeled data to be manipulated by the DataFrame
    public API class

    Attributes
    ----------
    shape
    ndim
    axes
    values
    items

    Methods
    -------
    set_axis(axis, new_labels)
    copy(deep=True)

    get_dtype_counts
    get_ftype_counts
    get_dtypes
    get_ftypes

    apply(func, axes, block_filter_fn)

    get_bool_data
    get_numeric_data

    get_slice(slice_like, axis)
    get(label)
    iget(loc)
    get_scalar(label_tup)

    take(indexer, axis)
    reindex_axis(new_labels, axis)
    reindex_indexer(new_labels, indexer, axis)

    delete(label)
    insert(loc, label, value)
    set(label, value)

    Parameters
    ----------


    Notes
    -----
    This is *not* a public API class
    """
    __slots__ = ['axes', 'blocks', '_ndim', '_shape', '_known_consolidated',
                 '_is_consolidated', '_blknos', '_blklocs']

    def __init__(self, blocks, axes, do_integrity_check=True):
        self.axes = [ensure_index(ax) for ax in axes]
        self.blocks = tuple(blocks)

        for block in blocks:
            if block.is_sparse:
                if len(block.mgr_locs) != 1:
                    raise AssertionError("Sparse block refers to multiple "
                                         "items")
            else:
                if self.ndim != block.ndim:
                    raise AssertionError(
                        'Number of Block dimensions ({block}) must equal '
                        'number of axes ({self})'.format(block=block.ndim,
                                                         self=self.ndim))

        if do_integrity_check:
            self._verify_integrity()

        self._consolidate_check()

        self._rebuild_blknos_and_blklocs()

    def make_empty(self, axes=None):
        """ return an empty BlockManager with the items axis of len 0 """
        if axes is None:
            axes = [ensure_index([])] + [ensure_index(a)
                                         for a in self.axes[1:]]

        # preserve dtype if possible
        if self.ndim == 1:
            blocks = np.array([], dtype=self.array_dtype)
        else:
            blocks = []
        return self.__class__(blocks, axes)

    def __nonzero__(self):
        return True

    # Python3 compat
    __bool__ = __nonzero__

    @property
    def shape(self):
        return tuple(len(ax) for ax in self.axes)

    @property
    def ndim(self):
        return len(self.axes)

    def set_axis(self, axis, new_labels):
        new_labels = ensure_index(new_labels)
        old_len = len(self.axes[axis])
        new_len = len(new_labels)

        if new_len != old_len:
            raise ValueError(
                'Length mismatch: Expected axis has {old} elements, new '
                'values have {new} elements'.format(old=old_len, new=new_len))

        self.axes[axis] = new_labels

    def rename_axis(self, mapper, axis, copy=True, level=None):
        """
        Rename one of axes.

        Parameters
        ----------
        mapper : unary callable
        axis : int
        copy : boolean, default True
        level : int, default None

        """
        obj = self.copy(deep=copy)
        obj.set_axis(axis, _transform_index(self.axes[axis], mapper, level))
        return obj

    def add_prefix(self, prefix):
        f = partial('{prefix}{}'.format, prefix=prefix)
        return self.rename_axis(f, axis=0)

    def add_suffix(self, suffix):
        f = partial('{}{suffix}'.format, suffix=suffix)
        return self.rename_axis(f, axis=0)

    @property
    def _is_single_block(self):
        if self.ndim == 1:
            return True

        if len(self.blocks) != 1:
            return False

        blk = self.blocks[0]
        return (blk.mgr_locs.is_slice_like and
                blk.mgr_locs.as_slice == slice(0, len(self), 1))

    def _rebuild_blknos_and_blklocs(self):
        """
        Update mgr._blknos / mgr._blklocs.
        """
        new_blknos = np.empty(self.shape[0], dtype=np.int64)
        new_blklocs = np.empty(self.shape[0], dtype=np.int64)
        new_blknos.fill(-1)
        new_blklocs.fill(-1)

        for blkno, blk in enumerate(self.blocks):
            rl = blk.mgr_locs
            new_blknos[rl.indexer] = blkno
            new_blklocs[rl.indexer] = np.arange(len(rl))

        if (new_blknos == -1).any():
            raise AssertionError("Gaps in blk ref_locs")

        self._blknos = new_blknos
        self._blklocs = new_blklocs

    # make items read only for now
    def _get_items(self):
        return self.axes[0]

    items = property(fget=_get_items)

    def _get_counts(self, f):
        """ return a dict of the counts of the function in BlockManager """
        self._consolidate_inplace()
        counts = dict()
        for b in self.blocks:
            v = f(b)
            counts[v] = counts.get(v, 0) + b.shape[0]
        return counts

    def get_dtype_counts(self):
        return self._get_counts(lambda b: b.dtype.name)

    def get_ftype_counts(self):
        return self._get_counts(lambda b: b.ftype)

    def get_dtypes(self):
        dtypes = np.array([blk.dtype for blk in self.blocks])
        return algos.take_1d(dtypes, self._blknos, allow_fill=False)

    def get_ftypes(self):
        ftypes = np.array([blk.ftype for blk in self.blocks])
        return algos.take_1d(ftypes, self._blknos, allow_fill=False)

    def __getstate__(self):
        block_values = [b.values for b in self.blocks]
        block_items = [self.items[b.mgr_locs.indexer] for b in self.blocks]
        axes_array = [ax for ax in self.axes]

        extra_state = {
            '0.14.1': {
                'axes': axes_array,
                'blocks': [dict(values=b.values, mgr_locs=b.mgr_locs.indexer)
                           for b in self.blocks]
            }
        }

        # First three elements of the state are to maintain forward
        # compatibility with 0.13.1.
        return axes_array, block_values, block_items, extra_state

    def __setstate__(self, state):
        def unpickle_block(values, mgr_locs):
            # numpy < 1.7 pickle compat
            if values.dtype == 'M8[us]':
                values = values.astype('M8[ns]')
            return make_block(values, placement=mgr_locs)

        if (isinstance(state, tuple) and len(state) >= 4 and
                '0.14.1' in state[3]):
            state = state[3]['0.14.1']
            self.axes = [ensure_index(ax) for ax in state['axes']]
            self.blocks = tuple(unpickle_block(b['values'], b['mgr_locs'])
                                for b in state['blocks'])
        else:
            # discard anything after 3rd, support beta pickling format for a
            # little while longer
            ax_arrays, bvalues, bitems = state[:3]

            self.axes = [ensure_index(ax) for ax in ax_arrays]

            if len(bitems) == 1 and self.axes[0].equals(bitems[0]):
                # This is a workaround for pre-0.14.1 pickles that didn't
                # support unpickling multi-block frames/panels with non-unique
                # columns/items, because given a manager with items ["a", "b",
                # "a"] there's no way of knowing which block's "a" is where.
                #
                # Single-block case can be supported under the assumption that
                # block items corresponded to manager items 1-to-1.
                all_mgr_locs = [slice(0, len(bitems[0]))]
            else:
                all_mgr_locs = [self.axes[0].get_indexer(blk_items)
                                for blk_items in bitems]

            self.blocks = tuple(
                unpickle_block(values, mgr_locs)
                for values, mgr_locs in zip(bvalues, all_mgr_locs))

        self._post_setstate()

    def _post_setstate(self):
        self._is_consolidated = False
        self._known_consolidated = False
        self._rebuild_blknos_and_blklocs()

    def __len__(self):
        return len(self.items)

    def __unicode__(self):
        output = pprint_thing(self.__class__.__name__)
        for i, ax in enumerate(self.axes):
            if i == 0:
                output += u('\nItems: {ax}'.format(ax=ax))
            else:
                output += u('\nAxis {i}: {ax}'.format(i=i, ax=ax))

        for block in self.blocks:
            output += u('\n{block}'.format(block=pprint_thing(block)))
        return output

    def _verify_integrity(self):
        mgr_shape = self.shape
        tot_items = sum(len(x.mgr_locs) for x in self.blocks)
        for block in self.blocks:
            if block._verify_integrity and block.shape[1:] != mgr_shape[1:]:
                construction_error(tot_items, block.shape[1:], self.axes)
        if len(self.items) != tot_items:
            raise AssertionError('Number of manager items must equal union of '
                                 'block items\n# manager items: {0}, # '
                                 'tot_items: {1}'.format(
                                     len(self.items), tot_items))

    def apply(self, f, axes=None, filter=None, do_integrity_check=False,
              consolidate=True, **kwargs):
        """
        iterate over the blocks, collect and create a new block manager

        Parameters
        ----------
        f : the callable or function name to operate on at the block level
        axes : optional (if not supplied, use self.axes)
        filter : list, if supplied, only call the block if the filter is in
                 the block
        do_integrity_check : boolean, default False. Do the block manager
            integrity check
        consolidate: boolean, default True. Join together blocks having same
            dtype

        Returns
        -------
        Block Manager (new object)

        """

        result_blocks = []

        # filter kwarg is used in replace-* family of methods
        if filter is not None:
            filter_locs = set(self.items.get_indexer_for(filter))
            if len(filter_locs) == len(self.items):
                # All items are included, as if there were no filtering
                filter = None
            else:
                kwargs['filter'] = filter_locs

        if consolidate:
            self._consolidate_inplace()

        if f == 'where':
            align_copy = True
            if kwargs.get('align', True):
                align_keys = ['other', 'cond']
            else:
                align_keys = ['cond']
        elif f == 'putmask':
            align_copy = False
            if kwargs.get('align', True):
                align_keys = ['new', 'mask']
            else:
                align_keys = ['mask']
        elif f == 'eval':
            align_copy = False
            align_keys = ['other']
        elif f == 'fillna':
            # fillna internally does putmask, maybe it's better to do this
            # at mgr, not block level?
            align_copy = False
            align_keys = ['value']
        else:
            align_keys = []

        # TODO(EA): may interfere with ExtensionBlock.setitem for blocks
        # with a .values attribute.
        aligned_args = dict((k, kwargs[k])
                            for k in align_keys
                            if hasattr(kwargs[k], 'values') and
                            not isinstance(kwargs[k], ABCExtensionArray))

        for b in self.blocks:
            if filter is not None:
                if not b.mgr_locs.isin(filter_locs).any():
                    result_blocks.append(b)
                    continue

            if aligned_args:
                b_items = self.items[b.mgr_locs.indexer]

                for k, obj in aligned_args.items():
                    axis = getattr(obj, '_info_axis_number', 0)
                    kwargs[k] = obj.reindex(b_items, axis=axis,
                                            copy=align_copy)

            kwargs['mgr'] = self
            applied = getattr(b, f)(**kwargs)
            result_blocks = _extend_blocks(applied, result_blocks)

        if len(result_blocks) == 0:
            return self.make_empty(axes or self.axes)
        bm = self.__class__(result_blocks, axes or self.axes,
                            do_integrity_check=do_integrity_check)
        bm._consolidate_inplace()
        return bm

    def reduction(self, f, axis=0, consolidate=True, transposed=False,
                  **kwargs):
        """
        iterate over the blocks, collect and create a new block manager.
        This routine is intended for reduction type operations and
        will do inference on the generated blocks.

        Parameters
        ----------
        f: the callable or function name to operate on at the block level
        axis: reduction axis, default 0
        consolidate: boolean, default True. Join together blocks having same
            dtype
        transposed: boolean, default False
            we are holding transposed data

        Returns
        -------
        Block Manager (new object)

        """

        if consolidate:
            self._consolidate_inplace()

        axes, blocks = [], []
        for b in self.blocks:
            kwargs['mgr'] = self
            axe, block = getattr(b, f)(axis=axis, **kwargs)

            axes.append(axe)
            blocks.append(block)

        # note that some DatetimeTZ, Categorical are always ndim==1
        ndim = {b.ndim for b in blocks}

        if 2 in ndim:

            new_axes = list(self.axes)

            # multiple blocks that are reduced
            if len(blocks) > 1:
                new_axes[1] = axes[0]

                # reset the placement to the original
                for b, sb in zip(blocks, self.blocks):
                    b.mgr_locs = sb.mgr_locs

            else:
                new_axes[axis] = Index(np.concatenate(
                    [ax.values for ax in axes]))

            if transposed:
                new_axes = new_axes[::-1]
                blocks = [b.make_block(b.values.T,
                                       placement=np.arange(b.shape[1])
                                       ) for b in blocks]

            return self.__class__(blocks, new_axes)

        # 0 ndim
        if 0 in ndim and 1 not in ndim:
            values = np.array([b.values for b in blocks])
            if len(values) == 1:
                return values.item()
            blocks = [make_block(values, ndim=1)]
            axes = Index([ax[0] for ax in axes])

        # single block
        values = _concat._concat_compat([b.values for b in blocks])

        # compute the orderings of our original data
        if len(self.blocks) > 1:

            indexer = np.empty(len(self.axes[0]), dtype=np.intp)
            i = 0
            for b in self.blocks:
                for j in b.mgr_locs:
                    indexer[j] = i
                    i = i + 1

            values = values.take(indexer)

        return SingleBlockManager(
            [make_block(values,
                        ndim=1,
                        placement=np.arange(len(values)))],
            axes[0])

    def isna(self, func, **kwargs):
        return self.apply('apply', func=func, **kwargs)

    def where(self, **kwargs):
        return self.apply('where', **kwargs)

    def eval(self, **kwargs):
        return self.apply('eval', **kwargs)

    def quantile(self, **kwargs):
        return self.reduction('quantile', **kwargs)

    def setitem(self, **kwargs):
        return self.apply('setitem', **kwargs)

    def putmask(self, **kwargs):
        return self.apply('putmask', **kwargs)

    def diff(self, **kwargs):
        return self.apply('diff', **kwargs)

    def interpolate(self, **kwargs):
        return self.apply('interpolate', **kwargs)

    def shift(self, **kwargs):
        return self.apply('shift', **kwargs)

    def fillna(self, **kwargs):
        return self.apply('fillna', **kwargs)

    def downcast(self, **kwargs):
        return self.apply('downcast', **kwargs)

    def astype(self, dtype, **kwargs):
        return self.apply('astype', dtype=dtype, **kwargs)

    def convert(self, **kwargs):
        return self.apply('convert', **kwargs)

    def replace(self, **kwargs):
        return self.apply('replace', **kwargs)

    def replace_list(self, src_list, dest_list, inplace=False, regex=False,
                     mgr=None):
        """ do a list replace """

        inplace = validate_bool_kwarg(inplace, 'inplace')

        if mgr is None:
            mgr = self

        # figure out our mask a-priori to avoid repeated replacements
        values = self.as_array()

        def comp(s):
            if isna(s):
                return isna(values)
            return _maybe_compare(values, getattr(s, 'asm8', s), operator.eq)

        masks = [comp(s) for i, s in enumerate(src_list)]

        result_blocks = []
        src_len = len(src_list) - 1
        for blk in self.blocks:

            # its possible to get multiple result blocks here
            # replace ALWAYS will return a list
            rb = [blk if inplace else blk.copy()]
            for i, (s, d) in enumerate(zip(src_list, dest_list)):
                new_rb = []
                for b in rb:
                    if b.dtype == np.object_:
                        convert = i == src_len
                        result = b.replace(s, d, inplace=inplace, regex=regex,
                                           mgr=mgr, convert=convert)
                        new_rb = _extend_blocks(result, new_rb)
                    else:
                        # get our mask for this element, sized to this
                        # particular block
                        m = masks[i][b.mgr_locs.indexer]
                        if m.any():
                            b = b.coerce_to_target_dtype(d)
                            new_rb.extend(b.putmask(m, d, inplace=True))
                        else:
                            new_rb.append(b)
                rb = new_rb
            result_blocks.extend(rb)

        bm = self.__class__(result_blocks, self.axes)
        bm._consolidate_inplace()
        return bm

    def reshape_nd(self, axes, **kwargs):
        """ a 2d-nd reshape operation on a BlockManager """
        return self.apply('reshape_nd', axes=axes, **kwargs)

    def is_consolidated(self):
        """
        Return True if more than one block with the same dtype
        """
        if not self._known_consolidated:
            self._consolidate_check()
        return self._is_consolidated

    def _consolidate_check(self):
        ftypes = [blk.ftype for blk in self.blocks]
        self._is_consolidated = len(ftypes) == len(set(ftypes))
        self._known_consolidated = True

    @property
    def is_mixed_type(self):
        # Warning, consolidation needs to get checked upstairs
        self._consolidate_inplace()
        return len(self.blocks) > 1

    @property
    def is_numeric_mixed_type(self):
        # Warning, consolidation needs to get checked upstairs
        self._consolidate_inplace()
        return all(block.is_numeric for block in self.blocks)

    @property
    def is_datelike_mixed_type(self):
        # Warning, consolidation needs to get checked upstairs
        self._consolidate_inplace()
        return any(block.is_datelike for block in self.blocks)

    @property
    def any_extension_types(self):
        """Whether any of the blocks in this manager are extension blocks"""
        return any(block.is_extension for block in self.blocks)

    @property
    def is_view(self):
        """ return a boolean if we are a single block and are a view """
        if len(self.blocks) == 1:
            return self.blocks[0].is_view

        # It is technically possible to figure out which blocks are views
        # e.g. [ b.values.base is not None for b in self.blocks ]
        # but then we have the case of possibly some blocks being a view
        # and some blocks not. setting in theory is possible on the non-view
        # blocks w/o causing a SettingWithCopy raise/warn. But this is a bit
        # complicated

        return False

    def get_bool_data(self, copy=False):
        """
        Parameters
        ----------
        copy : boolean, default False
            Whether to copy the blocks
        """
        self._consolidate_inplace()
        return self.combine([b for b in self.blocks if b.is_bool], copy)

    def get_numeric_data(self, copy=False):
        """
        Parameters
        ----------
        copy : boolean, default False
            Whether to copy the blocks
        """
        self._consolidate_inplace()
        return self.combine([b for b in self.blocks if b.is_numeric], copy)

    def combine(self, blocks, copy=True):
        """ return a new manager with the blocks """
        if len(blocks) == 0:
            return self.make_empty()

        # FIXME: optimization potential
        indexer = np.sort(np.concatenate([b.mgr_locs.as_array
                                          for b in blocks]))
        inv_indexer = lib.get_reverse_indexer(indexer, self.shape[0])

        new_blocks = []
        for b in blocks:
            b = b.copy(deep=copy)
            b.mgr_locs = algos.take_1d(inv_indexer, b.mgr_locs.as_array,
                                       axis=0, allow_fill=False)
            new_blocks.append(b)

        axes = list(self.axes)
        axes[0] = self.items.take(indexer)

        return self.__class__(new_blocks, axes, do_integrity_check=False)

    def get_slice(self, slobj, axis=0):
        if axis >= self.ndim:
            raise IndexError("Requested axis not found in manager")

        if axis == 0:
            new_blocks = self._slice_take_blocks_ax0(slobj)
        else:
            slicer = [slice(None)] * (axis + 1)
            slicer[axis] = slobj
            slicer = tuple(slicer)
            new_blocks = [blk.getitem_block(slicer) for blk in self.blocks]

        new_axes = list(self.axes)
        new_axes[axis] = new_axes[axis][slobj]

        bm = self.__class__(new_blocks, new_axes, do_integrity_check=False)
        bm._consolidate_inplace()
        return bm

    def __contains__(self, item):
        return item in self.items

    @property
    def nblocks(self):
        return len(self.blocks)

    def copy(self, deep=True, mgr=None):
        """
        Make deep or shallow copy of BlockManager

        Parameters
        ----------
        deep : boolean o rstring, default True
            If False, return shallow copy (do not copy data)
            If 'all', copy data and a deep copy of the index

        Returns
        -------
        copy : BlockManager
        """

        # this preserves the notion of view copying of axes
        if deep:
            if deep == 'all':
                copy = lambda ax: ax.copy(deep=True)
            else:
                copy = lambda ax: ax.view()
            new_axes = [copy(ax) for ax in self.axes]
        else:
            new_axes = list(self.axes)
        return self.apply('copy', axes=new_axes, deep=deep,
                          do_integrity_check=False)

    def as_array(self, transpose=False, items=None):
        """Convert the blockmanager data into an numpy array.

        Parameters
        ----------
        transpose : boolean, default False
            If True, transpose the return array
        items : list of strings or None
            Names of block items that will be included in the returned
            array. ``None`` means that all block items will be used

        Returns
        -------
        arr : ndarray
        """
        if len(self.blocks) == 0:
            arr = np.empty(self.shape, dtype=float)
            return arr.transpose() if transpose else arr

        if items is not None:
            mgr = self.reindex_axis(items, axis=0)
        else:
            mgr = self

        if self._is_single_block or not self.is_mixed_type:
            arr = mgr.blocks[0].get_values()
        else:
            arr = mgr._interleave()

        return arr.transpose() if transpose else arr

    def _interleave(self):
        """
        Return ndarray from blocks with specified item order
        Items must be contained in the blocks
        """
        dtype = _interleaved_dtype(self.blocks)

        result = np.empty(self.shape, dtype=dtype)

        if result.shape[0] == 0:
            # Workaround for numpy 1.7 bug:
            #
            #     >>> a = np.empty((0,10))
            #     >>> a[slice(0,0)]
            #     array([], shape=(0, 10), dtype=float64)
            #     >>> a[[]]
            #     Traceback (most recent call last):
            #       File "<stdin>", line 1, in <module>
            #     IndexError: index 0 is out of bounds for axis 0 with size 0
            return result

        itemmask = np.zeros(self.shape[0])

        for blk in self.blocks:
            rl = blk.mgr_locs
            result[rl.indexer] = blk.get_values(dtype)
            itemmask[rl.indexer] = 1

        if not itemmask.all():
            raise AssertionError('Some items were not contained in blocks')

        return result

    def to_dict(self, copy=True):
        """
        Return a dict of str(dtype) -> BlockManager

        Parameters
        ----------
        copy : boolean, default True

        Returns
        -------
        values : a dict of dtype -> BlockManager

        Notes
        -----
        This consolidates based on str(dtype)
        """
        self._consolidate_inplace()

        bd = {}
        for b in self.blocks:
            bd.setdefault(str(b.dtype), []).append(b)

        return {dtype: self.combine(blocks, copy=copy)
                for dtype, blocks in bd.items()}

    def xs(self, key, axis=1, copy=True, takeable=False):
        if axis < 1:
            raise AssertionError(
                'Can only take xs across axis >= 1, got {ax}'.format(ax=axis))

        # take by position
        if takeable:
            loc = key
        else:
            loc = self.axes[axis].get_loc(key)

        slicer = [slice(None, None) for _ in range(self.ndim)]
        slicer[axis] = loc
        slicer = tuple(slicer)

        new_axes = list(self.axes)

        # could be an array indexer!
        if isinstance(loc, (slice, np.ndarray)):
            new_axes[axis] = new_axes[axis][loc]
        else:
            new_axes.pop(axis)

        new_blocks = []
        if len(self.blocks) > 1:
            # we must copy here as we are mixed type
            for blk in self.blocks:
                newb = make_block(values=blk.values[slicer],
                                  klass=blk.__class__,
                                  placement=blk.mgr_locs)
                new_blocks.append(newb)
        elif len(self.blocks) == 1:
            block = self.blocks[0]
            vals = block.values[slicer]
            if copy:
                vals = vals.copy()
            new_blocks = [make_block(values=vals,
                                     placement=block.mgr_locs,
                                     klass=block.__class__)]

        return self.__class__(new_blocks, new_axes)

    def fast_xs(self, loc):
        """
        get a cross sectional for a given location in the
        items ; handle dups

        return the result, is *could* be a view in the case of a
        single block
        """
        if len(self.blocks) == 1:
            return self.blocks[0].iget((slice(None), loc))

        items = self.items

        # non-unique (GH4726)
        if not items.is_unique:
            result = self._interleave()
            if self.ndim == 2:
                result = result.T
            return result[loc]

        # unique
        dtype = _interleaved_dtype(self.blocks)
        n = len(items)
        result = np.empty(n, dtype=dtype)
        for blk in self.blocks:
            # Such assignment may incorrectly coerce NaT to None
            # result[blk.mgr_locs] = blk._slice((slice(None), loc))
            for i, rl in enumerate(blk.mgr_locs):
                result[rl] = blk._try_coerce_result(blk.iget((i, loc)))

        return result

    def consolidate(self):
        """
        Join together blocks having same dtype

        Returns
        -------
        y : BlockManager
        """
        if self.is_consolidated():
            return self

        bm = self.__class__(self.blocks, self.axes)
        bm._is_consolidated = False
        bm._consolidate_inplace()
        return bm

    def _consolidate_inplace(self):
        if not self.is_consolidated():
            self.blocks = tuple(_consolidate(self.blocks))
            self._is_consolidated = True
            self._known_consolidated = True
            self._rebuild_blknos_and_blklocs()

    def get(self, item, fastpath=True):
        """
        Return values for selected item (ndarray or BlockManager).
        """
        if self.items.is_unique:

            if not isna(item):
                loc = self.items.get_loc(item)
            else:
                indexer = np.arange(len(self.items))[isna(self.items)]

                # allow a single nan location indexer
                if not is_scalar(indexer):
                    if len(indexer) == 1:
                        loc = indexer.item()
                    else:
                        raise ValueError("cannot label index with a null key")

            return self.iget(loc, fastpath=fastpath)
        else:

            if isna(item):
                raise TypeError("cannot label index with a null key")

            indexer = self.items.get_indexer_for([item])
            return self.reindex_indexer(new_axis=self.items[indexer],
                                        indexer=indexer, axis=0,
                                        allow_dups=True)

    def iget(self, i, fastpath=True):
        """
        Return the data as a SingleBlockManager if fastpath=True and possible

        Otherwise return as a ndarray
        """
        block = self.blocks[self._blknos[i]]
        values = block.iget(self._blklocs[i])
        if not fastpath or not block._box_to_block_values or values.ndim != 1:
            return values

        # fastpath shortcut for select a single-dim from a 2-dim BM
        return SingleBlockManager(
            [block.make_block_same_class(values,
                                         placement=slice(0, len(values)),
                                         ndim=1)],
            self.axes[1])

    def get_scalar(self, tup):
        """
        Retrieve single item
        """
        full_loc = [ax.get_loc(x) for ax, x in zip(self.axes, tup)]
        blk = self.blocks[self._blknos[full_loc[0]]]
        values = blk.values

        # FIXME: this may return non-upcasted types?
        if values.ndim == 1:
            return values[full_loc[1]]

        full_loc[0] = self._blklocs[full_loc[0]]
        return values[tuple(full_loc)]

    def delete(self, item):
        """
        Delete selected item (items if non-unique) in-place.
        """
        indexer = self.items.get_loc(item)

        is_deleted = np.zeros(self.shape[0], dtype=np.bool_)
        is_deleted[indexer] = True
        ref_loc_offset = -is_deleted.cumsum()

        is_blk_deleted = [False] * len(self.blocks)

        if isinstance(indexer, int):
            affected_start = indexer
        else:
            affected_start = is_deleted.nonzero()[0][0]

        for blkno, _ in _fast_count_smallints(self._blknos[affected_start:]):
            blk = self.blocks[blkno]
            bml = blk.mgr_locs
            blk_del = is_deleted[bml.indexer].nonzero()[0]

            if len(blk_del) == len(bml):
                is_blk_deleted[blkno] = True
                continue
            elif len(blk_del) != 0:
                blk.delete(blk_del)
                bml = blk.mgr_locs

            blk.mgr_locs = bml.add(ref_loc_offset[bml.indexer])

        # FIXME: use Index.delete as soon as it uses fastpath=True
        self.axes[0] = self.items[~is_deleted]
        self.blocks = tuple(b for blkno, b in enumerate(self.blocks)
                            if not is_blk_deleted[blkno])
        self._shape = None
        self._rebuild_blknos_and_blklocs()

    def set(self, item, value, check=False):
        """
        Set new item in-place. Does not consolidate. Adds new Block if not
        contained in the current set of items
        if check, then validate that we are not setting the same data in-place
        """
        # FIXME: refactor, clearly separate broadcasting & zip-like assignment
        #        can prob also fix the various if tests for sparse/categorical

        # TODO(EA): Remove an is_extension_ when all extension types satisfy
        # the interface
        value_is_extension_type = (is_extension_type(value) or
                                   is_extension_array_dtype(value))

        # categorical/spares/datetimetz
        if value_is_extension_type:

            def value_getitem(placement):
                return value
        else:
            if value.ndim == self.ndim - 1:
                value = _safe_reshape(value, (1,) + value.shape)

                def value_getitem(placement):
                    return value
            else:

                def value_getitem(placement):
                    return value[placement.indexer]

            if value.shape[1:] != self.shape[1:]:
                raise AssertionError('Shape of new values must be compatible '
                                     'with manager shape')

        try:
            loc = self.items.get_loc(item)
        except KeyError:
            # This item wasn't present, just insert at end
            self.insert(len(self.items), item, value)
            return

        if isinstance(loc, int):
            loc = [loc]

        blknos = self._blknos[loc]
        blklocs = self._blklocs[loc].copy()

        unfit_mgr_locs = []
        unfit_val_locs = []
        removed_blknos = []
        for blkno, val_locs in _get_blkno_placements(blknos, len(self.blocks),
                                                     group=True):
            blk = self.blocks[blkno]
            blk_locs = blklocs[val_locs.indexer]
            if blk.should_store(value):
                blk.set(blk_locs, value_getitem(val_locs), check=check)
            else:
                unfit_mgr_locs.append(blk.mgr_locs.as_array[blk_locs])
                unfit_val_locs.append(val_locs)

                # If all block items are unfit, schedule the block for removal.
                if len(val_locs) == len(blk.mgr_locs):
                    removed_blknos.append(blkno)
                else:
                    self._blklocs[blk.mgr_locs.indexer] = -1
                    blk.delete(blk_locs)
                    self._blklocs[blk.mgr_locs.indexer] = np.arange(len(blk))

        if len(removed_blknos):
            # Remove blocks & update blknos accordingly
            is_deleted = np.zeros(self.nblocks, dtype=np.bool_)
            is_deleted[removed_blknos] = True

            new_blknos = np.empty(self.nblocks, dtype=np.int64)
            new_blknos.fill(-1)
            new_blknos[~is_deleted] = np.arange(self.nblocks -
                                                len(removed_blknos))
            self._blknos = algos.take_1d(new_blknos, self._blknos, axis=0,
                                         allow_fill=False)
            self.blocks = tuple(blk for i, blk in enumerate(self.blocks)
                                if i not in set(removed_blknos))

        if unfit_val_locs:
            unfit_mgr_locs = np.concatenate(unfit_mgr_locs)
            unfit_count = len(unfit_mgr_locs)

            new_blocks = []
            if value_is_extension_type:
                # This code (ab-)uses the fact that sparse blocks contain only
                # one item.
                new_blocks.extend(
                    make_block(values=value.copy(), ndim=self.ndim,
                               placement=slice(mgr_loc, mgr_loc + 1))
                    for mgr_loc in unfit_mgr_locs)

                self._blknos[unfit_mgr_locs] = (np.arange(unfit_count) +
                                                len(self.blocks))
                self._blklocs[unfit_mgr_locs] = 0

            else:
                # unfit_val_locs contains BlockPlacement objects
                unfit_val_items = unfit_val_locs[0].append(unfit_val_locs[1:])

                new_blocks.append(
                    make_block(values=value_getitem(unfit_val_items),
                               ndim=self.ndim, placement=unfit_mgr_locs))

                self._blknos[unfit_mgr_locs] = len(self.blocks)
                self._blklocs[unfit_mgr_locs] = np.arange(unfit_count)

            self.blocks += tuple(new_blocks)

            # Newly created block's dtype may already be present.
            self._known_consolidated = False

    def insert(self, loc, item, value, allow_duplicates=False):
        """
        Insert item at selected position.

        Parameters
        ----------
        loc : int
        item : hashable
        value : array_like
        allow_duplicates: bool
            If False, trying to insert non-unique item will raise

        """
        if not allow_duplicates and item in self.items:
            # Should this be a different kind of error??
            raise ValueError('cannot insert {}, already exists'.format(item))

        if not isinstance(loc, int):
            raise TypeError("loc must be int")

        # insert to the axis; this could possibly raise a TypeError
        new_axis = self.items.insert(loc, item)

        block = make_block(values=value, ndim=self.ndim,
                           placement=slice(loc, loc + 1))

        for blkno, count in _fast_count_smallints(self._blknos[loc:]):
            blk = self.blocks[blkno]
            if count == len(blk.mgr_locs):
                blk.mgr_locs = blk.mgr_locs.add(1)
            else:
                new_mgr_locs = blk.mgr_locs.as_array.copy()
                new_mgr_locs[new_mgr_locs >= loc] += 1
                blk.mgr_locs = new_mgr_locs

        if loc == self._blklocs.shape[0]:
            # np.append is a lot faster (at least in numpy 1.7.1), let's use it
            # if we can.
            self._blklocs = np.append(self._blklocs, 0)
            self._blknos = np.append(self._blknos, len(self.blocks))
        else:
            self._blklocs = np.insert(self._blklocs, loc, 0)
            self._blknos = np.insert(self._blknos, loc, len(self.blocks))

        self.axes[0] = new_axis
        self.blocks += (block,)
        self._shape = None

        self._known_consolidated = False

        if len(self.blocks) > 100:
            self._consolidate_inplace()

    def reindex_axis(self, new_index, axis, method=None, limit=None,
                     fill_value=None, copy=True):
        """
        Conform block manager to new index.
        """
        new_index = ensure_index(new_index)
        new_index, indexer = self.axes[axis].reindex(new_index, method=method,
                                                     limit=limit)

        return self.reindex_indexer(new_index, indexer, axis=axis,
                                    fill_value=fill_value, copy=copy)

    def reindex_indexer(self, new_axis, indexer, axis, fill_value=None,
                        allow_dups=False, copy=True):
        """
        Parameters
        ----------
        new_axis : Index
        indexer : ndarray of int64 or None
        axis : int
        fill_value : object
        allow_dups : bool

        pandas-indexer with -1's only.
        """
        if indexer is None:
            if new_axis is self.axes[axis] and not copy:
                return self

            result = self.copy(deep=copy)
            result.axes = list(self.axes)
            result.axes[axis] = new_axis
            return result

        self._consolidate_inplace()

        # some axes don't allow reindexing with dups
        if not allow_dups:
            self.axes[axis]._can_reindex(indexer)

        if axis >= self.ndim:
            raise IndexError("Requested axis not found in manager")

        if axis == 0:
            new_blocks = self._slice_take_blocks_ax0(indexer,
                                                     fill_tuple=(fill_value,))
        else:
            new_blocks = [blk.take_nd(indexer, axis=axis, fill_tuple=(
                fill_value if fill_value is not None else blk.fill_value,))
                for blk in self.blocks]

        new_axes = list(self.axes)
        new_axes[axis] = new_axis
        return self.__class__(new_blocks, new_axes)

    def _slice_take_blocks_ax0(self, slice_or_indexer, fill_tuple=None):
        """
        Slice/take blocks along axis=0.

        Overloaded for SingleBlock

        Returns
        -------
        new_blocks : list of Block

        """

        allow_fill = fill_tuple is not None

        sl_type, slobj, sllen = _preprocess_slice_or_indexer(
            slice_or_indexer, self.shape[0], allow_fill=allow_fill)

        if self._is_single_block:
            blk = self.blocks[0]

            if sl_type in ('slice', 'mask'):
                return [blk.getitem_block(slobj, new_mgr_locs=slice(0, sllen))]
            elif not allow_fill or self.ndim == 1:
                if allow_fill and fill_tuple[0] is None:
                    _, fill_value = maybe_promote(blk.dtype)
                    fill_tuple = (fill_value, )

                return [blk.take_nd(slobj, axis=0,
                                    new_mgr_locs=slice(0, sllen),
                                    fill_tuple=fill_tuple)]

        if sl_type in ('slice', 'mask'):
            blknos = self._blknos[slobj]
            blklocs = self._blklocs[slobj]
        else:
            blknos = algos.take_1d(self._blknos, slobj, fill_value=-1,
                                   allow_fill=allow_fill)
            blklocs = algos.take_1d(self._blklocs, slobj, fill_value=-1,
                                    allow_fill=allow_fill)

        # When filling blknos, make sure blknos is updated before appending to
        # blocks list, that way new blkno is exactly len(blocks).
        #
        # FIXME: mgr_groupby_blknos must return mgr_locs in ascending order,
        # pytables serialization will break otherwise.
        blocks = []
        for blkno, mgr_locs in _get_blkno_placements(blknos, len(self.blocks),
                                                     group=True):
            if blkno == -1:
                # If we've got here, fill_tuple was not None.
                fill_value = fill_tuple[0]

                blocks.append(self._make_na_block(placement=mgr_locs,
                                                  fill_value=fill_value))
            else:
                blk = self.blocks[blkno]

                # Otherwise, slicing along items axis is necessary.
                if not blk._can_consolidate:
                    # A non-consolidatable block, it's easy, because there's
                    # only one item and each mgr loc is a copy of that single
                    # item.
                    for mgr_loc in mgr_locs:
                        newblk = blk.copy(deep=True)
                        newblk.mgr_locs = slice(mgr_loc, mgr_loc + 1)
                        blocks.append(newblk)

                else:
                    blocks.append(blk.take_nd(blklocs[mgr_locs.indexer],
                                              axis=0, new_mgr_locs=mgr_locs,
                                              fill_tuple=None))

        return blocks

    def _make_na_block(self, placement, fill_value=None):
        # TODO: infer dtypes other than float64 from fill_value

        if fill_value is None:
            fill_value = np.nan
        block_shape = list(self.shape)
        block_shape[0] = len(placement)

        dtype, fill_value = infer_dtype_from_scalar(fill_value)
        block_values = np.empty(block_shape, dtype=dtype)
        block_values.fill(fill_value)
        return make_block(block_values, placement=placement)

    def take(self, indexer, axis=1, verify=True, convert=True):
        """
        Take items along any axis.
        """
        self._consolidate_inplace()
        indexer = (np.arange(indexer.start, indexer.stop, indexer.step,
                             dtype='int64')
                   if isinstance(indexer, slice)
                   else np.asanyarray(indexer, dtype='int64'))

        n = self.shape[axis]
        if convert:
            indexer = maybe_convert_indices(indexer, n)

        if verify:
            if ((indexer == -1) | (indexer >= n)).any():
                raise Exception('Indices must be nonzero and less than '
                                'the axis length')

        new_labels = self.axes[axis].take(indexer)
        return self.reindex_indexer(new_axis=new_labels, indexer=indexer,
                                    axis=axis, allow_dups=True)

    def merge(self, other, lsuffix='', rsuffix=''):
        if not self._is_indexed_like(other):
            raise AssertionError('Must have same axes to merge managers')

        l, r = items_overlap_with_suffix(left=self.items, lsuffix=lsuffix,
                                         right=other.items, rsuffix=rsuffix)
        new_items = _concat_indexes([l, r])

        new_blocks = [blk.copy(deep=False) for blk in self.blocks]

        offset = self.shape[0]
        for blk in other.blocks:
            blk = blk.copy(deep=False)
            blk.mgr_locs = blk.mgr_locs.add(offset)
            new_blocks.append(blk)

        new_axes = list(self.axes)
        new_axes[0] = new_items

        return self.__class__(_consolidate(new_blocks), new_axes)

    def _is_indexed_like(self, other):
        """
        Check all axes except items
        """
        if self.ndim != other.ndim:
            raise AssertionError(
                'Number of dimensions must agree got {ndim} and '
                '{oth_ndim}'.format(ndim=self.ndim, oth_ndim=other.ndim))
        for ax, oax in zip(self.axes[1:], other.axes[1:]):
            if not ax.equals(oax):
                return False
        return True

    def equals(self, other):
        self_axes, other_axes = self.axes, other.axes
        if len(self_axes) != len(other_axes):
            return False
        if not all(ax1.equals(ax2) for ax1, ax2 in zip(self_axes, other_axes)):
            return False
        self._consolidate_inplace()
        other._consolidate_inplace()
        if len(self.blocks) != len(other.blocks):
            return False

        # canonicalize block order, using a tuple combining the type
        # name and then mgr_locs because there might be unconsolidated
        # blocks (say, Categorical) which can only be distinguished by
        # the iteration order
        def canonicalize(block):
            return (block.dtype.name, block.mgr_locs.as_array.tolist())

        self_blocks = sorted(self.blocks, key=canonicalize)
        other_blocks = sorted(other.blocks, key=canonicalize)
        return all(block.equals(oblock)
                   for block, oblock in zip(self_blocks, other_blocks))

    def unstack(self, unstacker_func):
        """Return a blockmanager with all blocks unstacked.

        Parameters
        ----------
        unstacker_func : callable
            A (partially-applied) ``pd.core.reshape._Unstacker`` class.

        Returns
        -------
        unstacked : BlockManager
        """
        dummy = unstacker_func(np.empty((0, 0)), value_columns=self.items)
        new_columns = dummy.get_new_columns()
        new_index = dummy.get_new_index()
        new_blocks = []
        columns_mask = []

        for blk in self.blocks:
            blocks, mask = blk._unstack(
                partial(unstacker_func,
                        value_columns=self.items[blk.mgr_locs.indexer]),
                new_columns)

            new_blocks.extend(blocks)
            columns_mask.extend(mask)

        new_columns = new_columns[columns_mask]

        bm = BlockManager(new_blocks, [new_columns, new_index])
        return bm


class SingleBlockManager(BlockManager):
    """ manage a single block with """

    ndim = 1
    _is_consolidated = True
    _known_consolidated = True
    __slots__ = ()

    def __init__(self, block, axis, do_integrity_check=False, fastpath=False):

        if isinstance(axis, list):
            if len(axis) != 1:
                raise ValueError("cannot create SingleBlockManager with more "
                                 "than 1 axis")
            axis = axis[0]

        # passed from constructor, single block, single axis
        if fastpath:
            self.axes = [axis]
            if isinstance(block, list):

                # empty block
                if len(block) == 0:
                    block = [np.array([])]
                elif len(block) != 1:
                    raise ValueError('Cannot create SingleBlockManager with '
                                     'more than 1 block')
                block = block[0]
        else:
            self.axes = [ensure_index(axis)]

            # create the block here
            if isinstance(block, list):

                # provide consolidation to the interleaved_dtype
                if len(block) > 1:
                    dtype = _interleaved_dtype(block)
                    block = [b.astype(dtype) for b in block]
                    block = _consolidate(block)

                if len(block) != 1:
                    raise ValueError('Cannot create SingleBlockManager with '
                                     'more than 1 block')
                block = block[0]

        if not isinstance(block, Block):
            block = make_block(block, placement=slice(0, len(axis)), ndim=1)

        self.blocks = [block]

    def _post_setstate(self):
        pass

    @property
    def _block(self):
        return self.blocks[0]

    @property
    def _values(self):
        return self._block.values

    @property
    def _blknos(self):
        """ compat with BlockManager """
        return None

    @property
    def _blklocs(self):
        """ compat with BlockManager """
        return None

    def get_slice(self, slobj, axis=0):
        if axis >= self.ndim:
            raise IndexError("Requested axis not found in manager")

        return self.__class__(self._block._slice(slobj),
                              self.index[slobj], fastpath=True)

    @property
    def index(self):
        return self.axes[0]

    def convert(self, **kwargs):
        """ convert the whole block as one """
        kwargs['by_item'] = False
        return self.apply('convert', **kwargs)

    @property
    def dtype(self):
        return self._block.dtype

    @property
    def array_dtype(self):
        return self._block.array_dtype

    @property
    def ftype(self):
        return self._block.ftype

    def get_dtype_counts(self):
        return {self.dtype.name: 1}

    def get_ftype_counts(self):
        return {self.ftype: 1}

    def get_dtypes(self):
        return np.array([self._block.dtype])

    def get_ftypes(self):
        return np.array([self._block.ftype])

    def external_values(self):
        return self._block.external_values()

    def internal_values(self):
        return self._block.internal_values()

    def formatting_values(self):
        """Return the internal values used by the DataFrame/SeriesFormatter"""
        return self._block.formatting_values()

    def get_values(self):
        """ return a dense type view """
        return np.array(self._block.to_dense(), copy=False)

    @property
    def asobject(self):
        """
        return a object dtype array. datetime/timedelta like values are boxed
        to Timestamp/Timedelta instances.
        """
        return self._block.get_values(dtype=object)

    @property
    def _can_hold_na(self):
        return self._block._can_hold_na

    def is_consolidated(self):
        return True

    def _consolidate_check(self):
        pass

    def _consolidate_inplace(self):
        pass

    def delete(self, item):
        """
        Delete single item from SingleBlockManager.

        Ensures that self.blocks doesn't become empty.
        """
        loc = self.items.get_loc(item)
        self._block.delete(loc)
        self.axes[0] = self.axes[0].delete(loc)

    def fast_xs(self, loc):
        """
        fast path for getting a cross-section
        return a view of the data
        """
        return self._block.values[loc]

    def concat(self, to_concat, new_axis):
        """
        Concatenate a list of SingleBlockManagers into a single
        SingleBlockManager.

        Used for pd.concat of Series objects with axis=0.

        Parameters
        ----------
        to_concat : list of SingleBlockManagers
        new_axis : Index of the result

        Returns
        -------
        SingleBlockManager

        """
        non_empties = [x for x in to_concat if len(x) > 0]

        # check if all series are of the same block type:
        if len(non_empties) > 0:
            blocks = [obj.blocks[0] for obj in non_empties]

            if all(type(b) is type(blocks[0]) for b in blocks[1:]):  # noqa
                new_block = blocks[0].concat_same_type(blocks)
            else:
                values = [x.values for x in blocks]
                values = _concat._concat_compat(values)
                new_block = make_block(
                    values, placement=slice(0, len(values), 1))
        else:
            values = [x._block.values for x in to_concat]
            values = _concat._concat_compat(values)
            new_block = make_block(
                values, placement=slice(0, len(values), 1))

        mgr = SingleBlockManager(new_block, new_axis)
        return mgr


def construction_error(tot_items, block_shape, axes, e=None):
    """ raise a helpful message about our construction """
    passed = tuple(map(int, [tot_items] + list(block_shape)))
    implied = tuple(map(int, [len(ax) for ax in axes]))
    if passed == implied and e is not None:
        raise e
    if block_shape[0] == 0:
        raise ValueError("Empty data passed with indices specified.")
    raise ValueError("Shape of passed values is {0}, indices imply {1}".format(
        passed, implied))


def create_block_manager_from_blocks(blocks, axes):
    try:
        if len(blocks) == 1 and not isinstance(blocks[0], Block):
            # if blocks[0] is of length 0, return empty blocks
            if not len(blocks[0]):
                blocks = []
            else:
                # It's OK if a single block is passed as values, its placement
                # is basically "all items", but if there're many, don't bother
                # converting, it's an error anyway.
                blocks = [make_block(values=blocks[0],
                                     placement=slice(0, len(axes[0])))]

        mgr = BlockManager(blocks, axes)
        mgr._consolidate_inplace()
        return mgr

    except (ValueError) as e:
        blocks = [getattr(b, 'values', b) for b in blocks]
        tot_items = sum(b.shape[0] for b in blocks)
        construction_error(tot_items, blocks[0].shape[1:], axes, e)


def create_block_manager_from_arrays(arrays, names, axes):

    try:
        blocks = form_blocks(arrays, names, axes)
        mgr = BlockManager(blocks, axes)
        mgr._consolidate_inplace()
        return mgr
    except ValueError as e:
        construction_error(len(arrays), arrays[0].shape, axes, e)


def form_blocks(arrays, names, axes):
    # put "leftover" items in float bucket, where else?
    # generalize?
    items_dict = defaultdict(list)
    extra_locs = []

    names_idx = ensure_index(names)
    if names_idx.equals(axes[0]):
        names_indexer = np.arange(len(names_idx))
    else:
        assert names_idx.intersection(axes[0]).is_unique
        names_indexer = names_idx.get_indexer_for(axes[0])

    for i, name_idx in enumerate(names_indexer):
        if name_idx == -1:
            extra_locs.append(i)
            continue

        k = names[name_idx]
        v = arrays[name_idx]

        block_type = get_block_type(v)
        items_dict[block_type.__name__].append((i, k, v))

    blocks = []
    if len(items_dict['FloatBlock']):
        float_blocks = _multi_blockify(items_dict['FloatBlock'])
        blocks.extend(float_blocks)

    if len(items_dict['ComplexBlock']):
        complex_blocks = _multi_blockify(items_dict['ComplexBlock'])
        blocks.extend(complex_blocks)

    if len(items_dict['TimeDeltaBlock']):
        timedelta_blocks = _multi_blockify(items_dict['TimeDeltaBlock'])
        blocks.extend(timedelta_blocks)

    if len(items_dict['IntBlock']):
        int_blocks = _multi_blockify(items_dict['IntBlock'])
        blocks.extend(int_blocks)

    if len(items_dict['DatetimeBlock']):
        datetime_blocks = _simple_blockify(items_dict['DatetimeBlock'],
                                           _NS_DTYPE)
        blocks.extend(datetime_blocks)

    if len(items_dict['DatetimeTZBlock']):
        dttz_blocks = [make_block(array,
                                  klass=DatetimeTZBlock,
                                  placement=[i])
                       for i, _, array in items_dict['DatetimeTZBlock']]
        blocks.extend(dttz_blocks)

    if len(items_dict['BoolBlock']):
        bool_blocks = _simple_blockify(items_dict['BoolBlock'], np.bool_)
        blocks.extend(bool_blocks)

    if len(items_dict['ObjectBlock']) > 0:
        object_blocks = _simple_blockify(items_dict['ObjectBlock'], np.object_)
        blocks.extend(object_blocks)

    if len(items_dict['SparseBlock']) > 0:
        sparse_blocks = _sparse_blockify(items_dict['SparseBlock'])
        blocks.extend(sparse_blocks)

    if len(items_dict['CategoricalBlock']) > 0:
        cat_blocks = [make_block(array, klass=CategoricalBlock, placement=[i])
                      for i, _, array in items_dict['CategoricalBlock']]
        blocks.extend(cat_blocks)

    if len(items_dict['ExtensionBlock']):

        external_blocks = [
            make_block(array, klass=ExtensionBlock, placement=[i])
            for i, _, array in items_dict['ExtensionBlock']
        ]

        blocks.extend(external_blocks)

    if len(extra_locs):
        shape = (len(extra_locs),) + tuple(len(x) for x in axes[1:])

        # empty items -> dtype object
        block_values = np.empty(shape, dtype=object)
        block_values.fill(np.nan)

        na_block = make_block(block_values, placement=extra_locs)
        blocks.append(na_block)

    return blocks


def _simple_blockify(tuples, dtype):
    """ return a single array of a block that has a single dtype; if dtype is
    not None, coerce to this dtype
    """
    values, placement = _stack_arrays(tuples, dtype)

    # CHECK DTYPE?
    if dtype is not None and values.dtype != dtype:  # pragma: no cover
        values = values.astype(dtype)

    block = make_block(values, placement=placement)
    return [block]


def _multi_blockify(tuples, dtype=None):
    """ return an array of blocks that potentially have different dtypes """

    # group by dtype
    grouper = itertools.groupby(tuples, lambda x: x[2].dtype)

    new_blocks = []
    for dtype, tup_block in grouper:

        values, placement = _stack_arrays(list(tup_block), dtype)

        block = make_block(values, placement=placement)
        new_blocks.append(block)

    return new_blocks


def _sparse_blockify(tuples, dtype=None):
    """ return an array of blocks that potentially have different dtypes (and
    are sparse)
    """

    new_blocks = []
    for i, names, array in tuples:
        array = _maybe_to_sparse(array)
        block = make_block(array, klass=SparseBlock, placement=[i])
        new_blocks.append(block)

    return new_blocks


def _stack_arrays(tuples, dtype):

    # fml
    def _asarray_compat(x):
        if isinstance(x, ABCSeries):
            return x._values
        else:
            return np.asarray(x)

    def _shape_compat(x):
        if isinstance(x, ABCSeries):
            return len(x),
        else:
            return x.shape

    placement, names, arrays = zip(*tuples)

    first = arrays[0]
    shape = (len(arrays),) + _shape_compat(first)

    stacked = np.empty(shape, dtype=dtype)
    for i, arr in enumerate(arrays):
        stacked[i] = _asarray_compat(arr)

    return stacked, placement


def _interleaved_dtype(blocks):
    if not len(blocks):
        return None

    dtype = find_common_type([b.dtype for b in blocks])

    # only numpy compat
    if isinstance(dtype, (PandasExtensionDtype, ExtensionDtype)):
        dtype = np.object

    return dtype


def _consolidate(blocks):
    """
    Merge blocks having same dtype, exclude non-consolidating blocks
    """

    # sort by _can_consolidate, dtype
    gkey = lambda x: x._consolidate_key
    grouper = itertools.groupby(sorted(blocks, key=gkey), gkey)

    new_blocks = []
    for (_can_consolidate, dtype), group_blocks in grouper:
        merged_blocks = _merge_blocks(list(group_blocks), dtype=dtype,
                                      _can_consolidate=_can_consolidate)
        new_blocks = _extend_blocks(merged_blocks, new_blocks)
    return new_blocks


def _maybe_compare(a, b, op):

    is_a_array = isinstance(a, np.ndarray)
    is_b_array = isinstance(b, np.ndarray)

    # numpy deprecation warning to have i8 vs integer comparisons
    if is_datetimelike_v_numeric(a, b):
        result = False

    # numpy deprecation warning if comparing numeric vs string-like
    elif is_numeric_v_string_like(a, b):
        result = False

    else:
        result = op(a, b)

    if is_scalar(result) and (is_a_array or is_b_array):
        type_names = [type(a).__name__, type(b).__name__]

        if is_a_array:
            type_names[0] = 'ndarray(dtype={dtype})'.format(dtype=a.dtype)

        if is_b_array:
            type_names[1] = 'ndarray(dtype={dtype})'.format(dtype=b.dtype)

        raise TypeError(
            "Cannot compare types {a!r} and {b!r}".format(a=type_names[0],
                                                          b=type_names[1]))
    return result


def _concat_indexes(indexes):
    return indexes[0].append(indexes[1:])


def _get_blkno_placements(blknos, blk_count, group=True):
    """

    Parameters
    ----------
    blknos : array of int64
    blk_count : int
    group : bool

    Returns
    -------
    iterator
        yield (BlockPlacement, blkno)

    """

    blknos = ensure_int64(blknos)

    # FIXME: blk_count is unused, but it may avoid the use of dicts in cython
    for blkno, indexer in libinternals.get_blkno_indexers(blknos, group):
        yield blkno, BlockPlacement(indexer)


def items_overlap_with_suffix(left, lsuffix, right, rsuffix):
    """
    If two indices overlap, add suffixes to overlapping entries.

    If corresponding suffix is empty, the entry is simply converted to string.

    """
    to_rename = left.intersection(right)
    if len(to_rename) == 0:
        return left, right
    else:
        if not lsuffix and not rsuffix:
            raise ValueError('columns overlap but no suffix specified: '
                             '{rename}'.format(rename=to_rename))

        def lrenamer(x):
            if x in to_rename:
                return '{x}{lsuffix}'.format(x=x, lsuffix=lsuffix)
            return x

        def rrenamer(x):
            if x in to_rename:
                return '{x}{rsuffix}'.format(x=x, rsuffix=rsuffix)
            return x

        return (_transform_index(left, lrenamer),
                _transform_index(right, rrenamer))


def _transform_index(index, func, level=None):
    """
    Apply function to all values found in index.

    This includes transforming multiindex entries separately.
    Only apply function to one level of the MultiIndex if level is specified.

    """
    if isinstance(index, MultiIndex):
        if level is not None:
            items = [tuple(func(y) if i == level else y
                           for i, y in enumerate(x)) for x in index]
        else:
            items = [tuple(func(y) for y in x) for x in index]
        return MultiIndex.from_tuples(items, names=index.names)
    else:
        items = [func(x) for x in index]
        return Index(items, name=index.name, tupleize_cols=False)


def concatenate_block_managers(mgrs_indexers, axes, concat_axis, copy):
    """
    Concatenate block managers into one.

    Parameters
    ----------
    mgrs_indexers : list of (BlockManager, {axis: indexer,...}) tuples
    axes : list of Index
    concat_axis : int
    copy : bool

    """
    concat_plan = combine_concat_plans(
        [get_mgr_concatenation_plan(mgr, indexers)
         for mgr, indexers in mgrs_indexers], concat_axis)

    blocks = []

    for placement, join_units in concat_plan:

        if len(join_units) == 1 and not join_units[0].indexers:
            b = join_units[0].block
            values = b.values
            if copy:
                values = values.copy()
            elif not copy:
                values = values.view()
            b = b.make_block_same_class(values, placement=placement)
        elif is_uniform_join_units(join_units):
            b = join_units[0].block.concat_same_type(
                [ju.block for ju in join_units], placement=placement)
        else:
            b = make_block(
                concatenate_join_units(join_units, concat_axis, copy=copy),
                placement=placement)
        blocks.append(b)

    return BlockManager(blocks, axes)


def is_uniform_join_units(join_units):
    """
    Check if the join units consist of blocks of uniform type that can
    be concatenated using Block.concat_same_type instead of the generic
    concatenate_join_units (which uses `_concat._concat_compat`).

    """
    return (
        # all blocks need to have the same type
        all(type(ju.block) is type(join_units[0].block) for ju in join_units) and  # noqa
        # no blocks that would get missing values (can lead to type upcasts)
        # unless we're an extension dtype.
        all(not ju.is_na or ju.block.is_extension for ju in join_units) and
        # no blocks with indexers (as then the dimensions do not fit)
        all(not ju.indexers for ju in join_units) and
        # disregard Panels
        all(ju.block.ndim <= 2 for ju in join_units) and
        # only use this path when there is something to concatenate
        len(join_units) > 1)


def is_uniform_reindex(join_units):
    return (
        # TODO: should this be ju.block._can_hold_na?
        all(ju.block and ju.block.is_extension for ju in join_units) and
        len(set(ju.block.dtype.name for ju in join_units)) == 1
    )


def get_empty_dtype_and_na(join_units):
    """
    Return dtype and N/A values to use when concatenating specified units.

    Returned N/A value may be None which means there was no casting involved.

    Returns
    -------
    dtype
    na
    """

    if len(join_units) == 1:
        blk = join_units[0].block
        if blk is None:
            return np.float64, np.nan

    if is_uniform_reindex(join_units):
        # XXX: integrate property
        empty_dtype = join_units[0].block.dtype
        upcasted_na = join_units[0].block.fill_value
        return empty_dtype, upcasted_na

    has_none_blocks = False
    dtypes = [None] * len(join_units)
    for i, unit in enumerate(join_units):
        if unit.block is None:
            has_none_blocks = True
        else:
            dtypes[i] = unit.dtype

    upcast_classes = defaultdict(list)
    null_upcast_classes = defaultdict(list)
    for dtype, unit in zip(dtypes, join_units):
        if dtype is None:
            continue

        if is_categorical_dtype(dtype):
            upcast_cls = 'category'
        elif is_datetimetz(dtype):
            upcast_cls = 'datetimetz'
        elif issubclass(dtype.type, np.bool_):
            upcast_cls = 'bool'
        elif issubclass(dtype.type, np.object_):
            upcast_cls = 'object'
        elif is_datetime64_dtype(dtype):
            upcast_cls = 'datetime'
        elif is_timedelta64_dtype(dtype):
            upcast_cls = 'timedelta'
        elif is_float_dtype(dtype) or is_numeric_dtype(dtype):
            upcast_cls = dtype.name
        else:
            upcast_cls = 'float'

        # Null blocks should not influence upcast class selection, unless there
        # are only null blocks, when same upcasting rules must be applied to
        # null upcast classes.
        if unit.is_na:
            null_upcast_classes[upcast_cls].append(dtype)
        else:
            upcast_classes[upcast_cls].append(dtype)

    if not upcast_classes:
        upcast_classes = null_upcast_classes

    # create the result
    if 'object' in upcast_classes:
        return np.dtype(np.object_), np.nan
    elif 'bool' in upcast_classes:
        if has_none_blocks:
            return np.dtype(np.object_), np.nan
        else:
            return np.dtype(np.bool_), None
    elif 'category' in upcast_classes:
        return np.dtype(np.object_), np.nan
    elif 'datetimetz' in upcast_classes:
        dtype = upcast_classes['datetimetz']
        return dtype[0], tslibs.iNaT
    elif 'datetime' in upcast_classes:
        return np.dtype('M8[ns]'), tslibs.iNaT
    elif 'timedelta' in upcast_classes:
        return np.dtype('m8[ns]'), tslibs.iNaT
    else:  # pragma
        g = np.find_common_type(upcast_classes, [])
        if is_float_dtype(g):
            return g, g.type(np.nan)
        elif is_numeric_dtype(g):
            if has_none_blocks:
                return np.float64, np.nan
            else:
                return g, None

    msg = "invalid dtype determination in get_concat_dtype"
    raise AssertionError(msg)


def concatenate_join_units(join_units, concat_axis, copy):
    """
    Concatenate values from several join units along selected axis.
    """
    if concat_axis == 0 and len(join_units) > 1:
        # Concatenating join units along ax0 is handled in _merge_blocks.
        raise AssertionError("Concatenating join units along axis0")

    empty_dtype, upcasted_na = get_empty_dtype_and_na(join_units)

    to_concat = [ju.get_reindexed_values(empty_dtype=empty_dtype,
                                         upcasted_na=upcasted_na)
                 for ju in join_units]

    if len(to_concat) == 1:
        # Only one block, nothing to concatenate.
        concat_values = to_concat[0]
        if copy:
            if isinstance(concat_values, np.ndarray):
                # non-reindexed (=not yet copied) arrays are made into a view
                # in JoinUnit.get_reindexed_values
                if concat_values.base is not None:
                    concat_values = concat_values.copy()
            else:
                concat_values = concat_values.copy()
    else:
        concat_values = _concat._concat_compat(to_concat, axis=concat_axis)

    return concat_values


def get_mgr_concatenation_plan(mgr, indexers):
    """
    Construct concatenation plan for given block manager and indexers.

    Parameters
    ----------
    mgr : BlockManager
    indexers : dict of {axis: indexer}

    Returns
    -------
    plan : list of (BlockPlacement, JoinUnit) tuples

    """
    # Calculate post-reindex shape , save for item axis which will be separate
    # for each block anyway.
    mgr_shape = list(mgr.shape)
    for ax, indexer in indexers.items():
        mgr_shape[ax] = len(indexer)
    mgr_shape = tuple(mgr_shape)

    if 0 in indexers:
        ax0_indexer = indexers.pop(0)
        blknos = algos.take_1d(mgr._blknos, ax0_indexer, fill_value=-1)
        blklocs = algos.take_1d(mgr._blklocs, ax0_indexer, fill_value=-1)
    else:

        if mgr._is_single_block:
            blk = mgr.blocks[0]
            return [(blk.mgr_locs, JoinUnit(blk, mgr_shape, indexers))]

        ax0_indexer = None
        blknos = mgr._blknos
        blklocs = mgr._blklocs

    plan = []
    for blkno, placements in _get_blkno_placements(blknos, len(mgr.blocks),
                                                   group=False):

        assert placements.is_slice_like

        join_unit_indexers = indexers.copy()

        shape = list(mgr_shape)
        shape[0] = len(placements)
        shape = tuple(shape)

        if blkno == -1:
            unit = JoinUnit(None, shape)
        else:
            blk = mgr.blocks[blkno]
            ax0_blk_indexer = blklocs[placements.indexer]

            unit_no_ax0_reindexing = (len(placements) == len(blk.mgr_locs) and
                                      # Fastpath detection of join unit not
                                      # needing to reindex its block: no ax0
                                      # reindexing took place and block
                                      # placement was sequential before.
                                      ((ax0_indexer is None and
                                        blk.mgr_locs.is_slice_like and
                                        blk.mgr_locs.as_slice.step == 1) or
                                       # Slow-ish detection: all indexer locs
                                       # are sequential (and length match is
                                       # checked above).
                                       (np.diff(ax0_blk_indexer) == 1).all()))

            # Omit indexer if no item reindexing is required.
            if unit_no_ax0_reindexing:
                join_unit_indexers.pop(0, None)
            else:
                join_unit_indexers[0] = ax0_blk_indexer

            unit = JoinUnit(blk, shape, join_unit_indexers)

        plan.append((placements, unit))

    return plan


def combine_concat_plans(plans, concat_axis):
    """
    Combine multiple concatenation plans into one.

    existing_plan is updated in-place.
    """
    if len(plans) == 1:
        for p in plans[0]:
            yield p[0], [p[1]]

    elif concat_axis == 0:
        offset = 0
        for plan in plans:
            last_plc = None

            for plc, unit in plan:
                yield plc.add(offset), [unit]
                last_plc = plc

            if last_plc is not None:
                offset += last_plc.as_slice.stop

    else:
        num_ended = [0]

        def _next_or_none(seq):
            retval = next(seq, None)
            if retval is None:
                num_ended[0] += 1
            return retval

        plans = list(map(iter, plans))
        next_items = list(map(_next_or_none, plans))

        while num_ended[0] != len(next_items):
            if num_ended[0] > 0:
                raise ValueError("Plan shapes are not aligned")

            placements, units = zip(*next_items)

            lengths = list(map(len, placements))
            min_len, max_len = min(lengths), max(lengths)

            if min_len == max_len:
                yield placements[0], units
                next_items[:] = map(_next_or_none, plans)
            else:
                yielded_placement = None
                yielded_units = [None] * len(next_items)
                for i, (plc, unit) in enumerate(next_items):
                    yielded_units[i] = unit
                    if len(plc) > min_len:
                        # trim_join_unit updates unit in place, so only
                        # placement needs to be sliced to skip min_len.
                        next_items[i] = (plc[min_len:],
                                         trim_join_unit(unit, min_len))
                    else:
                        yielded_placement = plc
                        next_items[i] = _next_or_none(plans[i])

                yield yielded_placement, yielded_units


def trim_join_unit(join_unit, length):
    """
    Reduce join_unit's shape along item axis to length.

    Extra items that didn't fit are returned as a separate block.
    """

    if 0 not in join_unit.indexers:
        extra_indexers = join_unit.indexers

        if join_unit.block is None:
            extra_block = None
        else:
            extra_block = join_unit.block.getitem_block(slice(length, None))
            join_unit.block = join_unit.block.getitem_block(slice(length))
    else:
        extra_block = join_unit.block

        extra_indexers = copy.copy(join_unit.indexers)
        extra_indexers[0] = extra_indexers[0][length:]
        join_unit.indexers[0] = join_unit.indexers[0][:length]

    extra_shape = (join_unit.shape[0] - length,) + join_unit.shape[1:]
    join_unit.shape = (length,) + join_unit.shape[1:]

    return JoinUnit(block=extra_block, indexers=extra_indexers,
                    shape=extra_shape)


class JoinUnit(object):

    def __init__(self, block, shape, indexers=None):
        # Passing shape explicitly is required for cases when block is None.
        if indexers is None:
            indexers = {}
        self.block = block
        self.indexers = indexers
        self.shape = shape

    def __repr__(self):
        return '{name}({block!r}, {indexers})'.format(
            name=self.__class__.__name__, block=self.block,
            indexers=self.indexers)

    @cache_readonly
    def needs_filling(self):
        for indexer in self.indexers.values():
            # FIXME: cache results of indexer == -1 checks.
            if (indexer == -1).any():
                return True

        return False

    @cache_readonly
    def dtype(self):
        if self.block is None:
            raise AssertionError("Block is None, no dtype")

        if not self.needs_filling:
            return self.block.dtype
        else:
            return _get_dtype(maybe_promote(self.block.dtype,
                                            self.block.fill_value)[0])

    @cache_readonly
    def is_na(self):
        if self.block is None:
            return True

        if not self.block._can_hold_na:
            return False

        # Usually it's enough to check but a small fraction of values to see if
        # a block is NOT null, chunks should help in such cases.  1000 value
        # was chosen rather arbitrarily.
        values = self.block.values
        if self.block.is_categorical:
            values_flat = values.categories
        elif self.block.is_sparse:
            # fill_value is not NaN and have holes
            if not values._null_fill_value and values.sp_index.ngaps > 0:
                return False
            values_flat = values.ravel(order='K')
        elif isinstance(self.block, ExtensionBlock):
            values_flat = values
        else:
            values_flat = values.ravel(order='K')
        total_len = values_flat.shape[0]
        chunk_len = max(total_len // 40, 1000)
        for i in range(0, total_len, chunk_len):
            if not isna(values_flat[i:i + chunk_len]).all():
                return False

        return True

    def get_reindexed_values(self, empty_dtype, upcasted_na):
        if upcasted_na is None:
            # No upcasting is necessary
            fill_value = self.block.fill_value
            values = self.block.get_values()
        else:
            fill_value = upcasted_na

            if self.is_na:
                if getattr(self.block, 'is_object', False):
                    # we want to avoid filling with np.nan if we are
                    # using None; we already know that we are all
                    # nulls
                    values = self.block.values.ravel(order='K')
                    if len(values) and values[0] is None:
                        fill_value = None

                if getattr(self.block, 'is_datetimetz', False) or \
                        is_datetimetz(empty_dtype):
                    pass
                elif getattr(self.block, 'is_categorical', False):
                    pass
                elif getattr(self.block, 'is_sparse', False):
                    pass
                else:
                    missing_arr = np.empty(self.shape, dtype=empty_dtype)
                    missing_arr.fill(fill_value)
                    return missing_arr

            if not self.indexers:
                if not self.block._can_consolidate:
                    # preserve these for validation in _concat_compat
                    return self.block.values

            if self.block.is_bool and not self.block.is_categorical:
                # External code requested filling/upcasting, bool values must
                # be upcasted to object to avoid being upcasted to numeric.
                values = self.block.astype(np.object_).values
            elif self.block.is_extension:
                values = self.block.values
            else:
                # No dtype upcasting is done here, it will be performed during
                # concatenation itself.
                values = self.block.get_values()

        if not self.indexers:
            # If there's no indexing to be done, we want to signal outside
            # code that this array must be copied explicitly.  This is done
            # by returning a view and checking `retval.base`.
            values = values.view()

        else:
            for ax, indexer in self.indexers.items():
                values = algos.take_nd(values, indexer, axis=ax,
                                       fill_value=fill_value)

        return values


def _fast_count_smallints(arr):
    """Faster version of set(arr) for sequences of small numbers."""
    if len(arr) == 0:
        # Handle empty arr case separately: numpy 1.6 chokes on that.
        return np.empty((0, 2), dtype=arr.dtype)
    else:
        counts = np.bincount(arr.astype(np.int_))
        nz = counts.nonzero()[0]
        return np.c_[nz, counts[nz]]


def _preprocess_slice_or_indexer(slice_or_indexer, length, allow_fill):
    if isinstance(slice_or_indexer, slice):
        return ('slice', slice_or_indexer,
                libinternals.slice_len(slice_or_indexer, length))
    elif (isinstance(slice_or_indexer, np.ndarray) and
          slice_or_indexer.dtype == np.bool_):
        return 'mask', slice_or_indexer, slice_or_indexer.sum()
    else:
        indexer = np.asanyarray(slice_or_indexer, dtype=np.int64)
        if not allow_fill:
            indexer = maybe_convert_indices(indexer, length)
        return 'fancy', indexer, len(indexer)
