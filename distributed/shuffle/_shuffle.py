from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Union

import toolz

from dask.base import tokenize
from dask.highlevelgraph import HighLevelGraph
from dask.layers import Layer

from distributed.core import PooledRPCCall
from distributed.exceptions import Reschedule
from distributed.shuffle._arrow import (
    check_dtype_support,
    check_minimal_arrow_version,
    convert_partition,
    list_of_buffers_to_table,
    serialize_table,
)
from distributed.shuffle._core import (
    NDIndex,
    ShuffleId,
    ShuffleRun,
    ShuffleState,
    ShuffleType,
    barrier_key,
    get_worker_plugin,
)
from distributed.shuffle._exceptions import ShuffleClosedError
from distributed.shuffle._limiter import ResourceLimiter
from distributed.sizeof import sizeof

logger = logging.getLogger("distributed.shuffle")
if TYPE_CHECKING:
    import pandas as pd
    import pyarrow as pa

    # TODO import from typing (requires Python >=3.10)
    from typing_extensions import TypeAlias

    from dask.dataframe import DataFrame


def shuffle_transfer(
    input: pd.DataFrame,
    id: ShuffleId,
    input_partition: int,
    npartitions: int,
    column: str,
    parts_out: set[int],
) -> int:
    try:
        return get_worker_plugin().add_partition(
            input,
            shuffle_id=id,
            type=ShuffleType.DATAFRAME,
            partition_id=input_partition,
            npartitions=npartitions,
            column=column,
            parts_out=parts_out,
        )
    except ShuffleClosedError:
        raise Reschedule()
    except Exception as e:
        raise RuntimeError(f"shuffle_transfer failed during shuffle {id}") from e


def shuffle_unpack(
    id: ShuffleId, output_partition: int, barrier_run_id: int, meta: pd.DataFrame
) -> pd.DataFrame:
    try:
        return get_worker_plugin().get_output_partition(
            id, barrier_run_id, output_partition, meta=meta
        )
    except Reschedule as e:
        raise e
    except ShuffleClosedError:
        raise Reschedule()
    except Exception as e:
        raise RuntimeError(f"shuffle_unpack failed during shuffle {id}") from e


def shuffle_barrier(id: ShuffleId, run_ids: list[int]) -> int:
    try:
        return get_worker_plugin().barrier(id, run_ids)
    except Exception as e:
        raise RuntimeError(f"shuffle_barrier failed during shuffle {id}") from e


def rearrange_by_column_p2p(
    df: DataFrame,
    column: str,
    npartitions: int | None = None,
) -> DataFrame:
    from dask.dataframe import DataFrame

    meta = df._meta
    check_dtype_support(meta)
    npartitions = npartitions or df.npartitions
    token = tokenize(df, column, npartitions)

    if any(not isinstance(c, str) for c in meta.columns):
        unsupported = {c: type(c) for c in meta.columns if not isinstance(c, str)}
        raise TypeError(
            f"p2p requires all column names to be str, found: {unsupported}",
        )

    name = f"shuffle-p2p-{token}"
    layer = P2PShuffleLayer(
        name,
        column,
        npartitions,
        npartitions_input=df.npartitions,
        name_input=df._name,
        meta_input=meta,
    )
    return DataFrame(
        HighLevelGraph.from_collections(name, layer, [df]),
        name,
        meta,
        [None] * (npartitions + 1),
    )


_T_Key: TypeAlias = Union[tuple[str, int], str]
_T_LowLevelGraph: TypeAlias = dict[_T_Key, tuple]


class P2PShuffleLayer(Layer):
    def __init__(
        self,
        name: str,
        column: str,
        npartitions: int,
        npartitions_input: int,
        name_input: str,
        meta_input: pd.DataFrame,
        parts_out: Iterable | None = None,
        annotations: dict | None = None,
    ):
        check_minimal_arrow_version()
        self.name = name
        self.column = column
        self.npartitions = npartitions
        self.name_input = name_input
        self.meta_input = meta_input
        if parts_out:
            self.parts_out = set(parts_out)
        else:
            self.parts_out = set(range(self.npartitions))
        self.npartitions_input = npartitions_input
        annotations = annotations or {}
        annotations.update({"shuffle": lambda key: key[1]})
        super().__init__(annotations=annotations)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}<name='{self.name}', npartitions={self.npartitions}>"
        )

    def get_output_keys(self) -> set[_T_Key]:
        return {(self.name, part) for part in self.parts_out}

    def is_materialized(self) -> bool:
        return hasattr(self, "_cached_dict")

    @property
    def _dict(self) -> _T_LowLevelGraph:
        """Materialize full dict representation"""
        self._cached_dict: _T_LowLevelGraph
        dsk: _T_LowLevelGraph
        if hasattr(self, "_cached_dict"):
            return self._cached_dict
        else:
            dsk = self._construct_graph()
            self._cached_dict = dsk
        return self._cached_dict

    def __getitem__(self, key: _T_Key) -> tuple:
        return self._dict[key]

    def __iter__(self) -> Iterator[_T_Key]:
        return iter(self._dict)

    def __len__(self) -> int:
        return len(self._dict)

    def _cull(self, parts_out: Iterable[int]) -> P2PShuffleLayer:
        return P2PShuffleLayer(
            self.name,
            self.column,
            self.npartitions,
            self.npartitions_input,
            self.name_input,
            self.meta_input,
            parts_out=parts_out,
        )

    def _keys_to_parts(self, keys: Iterable[_T_Key]) -> set[int]:
        """Simple utility to convert keys to partition indices."""
        parts = set()
        for key in keys:
            if isinstance(key, tuple) and len(key) == 2:
                _name, _part = key
                if _name != self.name:
                    continue
                parts.add(_part)
        return parts

    def cull(
        self, keys: Iterable[_T_Key], all_keys: Any
    ) -> tuple[P2PShuffleLayer, dict]:
        """Cull a P2PShuffleLayer HighLevelGraph layer.

        The underlying graph will only include the necessary
        tasks to produce the keys (indices) included in `parts_out`.
        Therefore, "culling" the layer only requires us to reset this
        parameter.
        """
        parts_out = self._keys_to_parts(keys)
        input_parts = {(self.name_input, i) for i in range(self.npartitions_input)}
        culled_deps = {(self.name, part): input_parts.copy() for part in parts_out}

        if parts_out != set(self.parts_out):
            culled_layer = self._cull(parts_out)
            return culled_layer, culled_deps
        else:
            return self, culled_deps

    def _construct_graph(self) -> _T_LowLevelGraph:
        token = tokenize(self.name_input, self.column, self.npartitions, self.parts_out)
        dsk: _T_LowLevelGraph = {}
        _barrier_key = barrier_key(ShuffleId(token))
        name = "shuffle-transfer-" + token
        transfer_keys = list()
        for i in range(self.npartitions_input):
            transfer_keys.append((name, i))
            dsk[(name, i)] = (
                shuffle_transfer,
                (self.name_input, i),
                token,
                i,
                self.npartitions,
                self.column,
                self.parts_out,
            )

        dsk[_barrier_key] = (shuffle_barrier, token, transfer_keys)

        name = self.name
        for part_out in self.parts_out:
            dsk[(name, part_out)] = (
                shuffle_unpack,
                token,
                part_out,
                _barrier_key,
                self.meta_input,
            )
        return dsk


def split_by_worker(
    df: pd.DataFrame,
    column: str,
    worker_for: pd.Series,
) -> dict[Any, pa.Table]:
    """
    Split data into many arrow batches, partitioned by destination worker
    """
    import numpy as np
    import pyarrow as pa

    df = df.merge(
        right=worker_for.cat.codes.rename("_worker"),
        left_on=column,
        right_index=True,
        how="inner",
    )
    nrows = len(df)
    if not nrows:
        return {}
    # assert len(df) == nrows  # Not true if some outputs aren't wanted
    # FIXME: If we do not preserve the index something is corrupting the
    # bytestream such that it cannot be deserialized anymore
    t = pa.Table.from_pandas(df, preserve_index=True)
    t = t.sort_by("_worker")
    codes = np.asarray(t["_worker"])
    t = t.drop(["_worker"])
    del df

    splits = np.where(codes[1:] != codes[:-1])[0] + 1
    splits = np.concatenate([[0], splits])

    shards = [
        t.slice(offset=a, length=b - a) for a, b in toolz.sliding_window(2, splits)
    ]
    shards.append(t.slice(offset=splits[-1], length=None))

    unique_codes = codes[splits]
    out = {
        # FIXME https://github.com/pandas-dev/pandas-stubs/issues/43
        worker_for.cat.categories[code]: shard
        for code, shard in zip(unique_codes, shards)
    }
    assert sum(map(len, out.values())) == nrows
    return out


def split_by_partition(t: pa.Table, column: str) -> dict[Any, pa.Table]:
    """
    Split data into many arrow batches, partitioned by final partition
    """
    import numpy as np

    partitions = t.select([column]).to_pandas()[column].unique()
    partitions.sort()
    t = t.sort_by(column)

    partition = np.asarray(t[column])
    splits = np.where(partition[1:] != partition[:-1])[0] + 1
    splits = np.concatenate([[0], splits])

    shards = [
        t.slice(offset=a, length=b - a) for a, b in toolz.sliding_window(2, splits)
    ]
    shards.append(t.slice(offset=splits[-1], length=None))
    assert len(t) == sum(map(len, shards))
    assert len(partitions) == len(shards)
    return dict(zip(partitions, shards))


class DataFrameShuffleRun(ShuffleRun[int, "pd.DataFrame"]):
    """State for a single active shuffle execution

    This object is responsible for splitting, sending, receiving and combining
    data shards.

    It is entirely agnostic to the distributed system and can perform a shuffle
    with other `Shuffle` instances using `rpc` and `broadcast`.

    The user of this needs to guarantee that only `Shuffle`s of the same unique
    `ShuffleID` interact.

    Parameters
    ----------
    worker_for:
        A mapping partition_id -> worker_address.
    output_workers:
        A set of all participating worker (addresses).
    column:
        The data column we split the input partition by.
    id:
        A unique `ShuffleID` this belongs to.
    run_id:
        A unique identifier of the specific execution of the shuffle this belongs to.
    local_address:
        The local address this Shuffle can be contacted by using `rpc`.
    directory:
        The scratch directory to buffer data in.
    executor:
        Thread pool to use for offloading compute.
    loop:
        The event loop.
    rpc:
        A callable returning a PooledRPCCall to contact other Shuffle instances.
        Typically a ConnectionPool.
    scheduler:
        A PooledRPCCall to to contact the scheduler.
    memory_limiter_disk:
    memory_limiter_comm:
        A ``ResourceLimiter`` limiting the total amount of memory used in either
        buffer.
    """

    def __init__(
        self,
        worker_for: dict[int, str],
        output_workers: set,
        column: str,
        id: ShuffleId,
        run_id: int,
        local_address: str,
        directory: str,
        executor: ThreadPoolExecutor,
        rpc: Callable[[str], PooledRPCCall],
        scheduler: PooledRPCCall,
        memory_limiter_disk: ResourceLimiter,
        memory_limiter_comms: ResourceLimiter,
    ):
        import pandas as pd

        super().__init__(
            id=id,
            run_id=run_id,
            output_workers=output_workers,
            local_address=local_address,
            directory=directory,
            executor=executor,
            rpc=rpc,
            scheduler=scheduler,
            memory_limiter_comms=memory_limiter_comms,
            memory_limiter_disk=memory_limiter_disk,
        )
        self.column = column
        partitions_of = defaultdict(list)
        for part, addr in worker_for.items():
            partitions_of[addr].append(part)
        self.partitions_of = dict(partitions_of)
        self.worker_for = pd.Series(worker_for, name="_workers").astype("category")

    async def receive(self, data: list[tuple[int, bytes]]) -> None:
        await self._receive(data)

    async def _receive(self, data: list[tuple[int, bytes]]) -> None:
        self.raise_if_closed()

        filtered = []
        for d in data:
            if d[0] not in self.received:
                filtered.append(d[1])
                self.received.add(d[0])
                self.total_recvd += sizeof(d)
        del data
        if not filtered:
            return
        try:
            groups = await self.offload(self._repartition_buffers, filtered)
            del filtered
            await self._write_to_disk(groups)
        except Exception as e:
            self._exception = e
            raise

    def _repartition_buffers(self, data: list[bytes]) -> dict[NDIndex, bytes]:
        table = list_of_buffers_to_table(data)
        groups = split_by_partition(table, self.column)
        assert len(table) == sum(map(len, groups.values()))
        del data
        return {(k,): serialize_table(v) for k, v in groups.items()}

    async def add_partition(self, data: pd.DataFrame, partition_id: int) -> int:
        self.raise_if_closed()
        if self.transferred:
            raise RuntimeError(f"Cannot add more partitions to {self}")

        def _() -> dict[str, tuple[int, bytes]]:
            out = split_by_worker(
                data,
                self.column,
                self.worker_for,
            )
            out = {k: (partition_id, serialize_table(t)) for k, t in out.items()}
            return out

        out = await self.offload(_)
        await self._write_to_comm(out)
        return self.run_id

    async def get_output_partition(
        self, partition_id: int, key: str, meta: pd.DataFrame | None = None
    ) -> pd.DataFrame:
        self.raise_if_closed()
        assert meta is not None
        assert self.transferred, "`get_output_partition` called before barrier task"

        await self._ensure_output_worker(partition_id, key)

        await self.flush_receive()
        try:
            data = self._read_from_disk((partition_id,))

            def _() -> pd.DataFrame:
                return convert_partition(data, meta)  # type: ignore

            out = await self.offload(_)
        except KeyError:
            out = meta.copy()
        return out

    def _get_assigned_worker(self, id: int) -> str:
        return self.worker_for[id]


@dataclass(eq=False)
class DataFrameShuffleState(ShuffleState):
    type: ClassVar[ShuffleType] = ShuffleType.DATAFRAME
    worker_for: dict[int, str]
    column: str

    def to_msg(self) -> dict[str, Any]:
        return {
            "status": "OK",
            "type": DataFrameShuffleState.type,
            "run_id": self.run_id,
            "worker_for": self.worker_for,
            "column": self.column,
            "output_workers": self.output_workers,
        }


def get_worker_for_range_sharding(
    npartitions: int, output_partition: int, workers: Sequence[str]
) -> str:
    """Get address of target worker for this output partition using range sharding"""
    i = len(workers) * output_partition // npartitions
    return workers[i]
