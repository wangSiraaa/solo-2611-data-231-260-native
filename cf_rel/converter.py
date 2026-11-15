"""核心转换逻辑：逐个 NetCDF 文件核对语义并产出关系表。

设计约束（见 README）：

* mapping.json 只提供变量**名称**；维度、instance_dimension、
  sample_dimension、cf_role、_FillValue/missing_value 全部以文件为准核对。
* 关系链只有两层：trajectory -> profile -> observation。
  profile.trajectoryIndex（0 基）指向轨迹；观测层二选一：
  contiguous 用 profile.rowSize 连续分段，indexed 用 obs.profileIndex
  显式索引，两种布局互斥。
* 所有归属判断只走索引链，绝不按行号对齐或按 ID 排序猜测。
* 不做插值、轨迹匹配或任何科学推断。
* 所有校验在任何最终关系表写出之前完成；写出采用整目录原子替换。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import netCDF4
import numpy as np

from .errors import ConversionError, UnsupportedFeatureError, ValidationError

# 关系表名
TRAJECTORY_TABLE = "trajectory"
PROFILE_TABLE = "profile"
OBSERVATION_TABLE = "observation"

# 布局
LAYOUT_CONTIGUOUS = "contiguous"
LAYOUT_INDEXED = "indexed"

# 本题只支持 standard/gregorian 日历、seconds since 参考点
SUPPORTED_CALENDARS = {"standard", "gregorian"}


@dataclass
class ConversionResult:
    """单次转换的结果摘要。"""

    source: str
    output_dir: str
    layout: str
    n_trajectories: int
    n_profiles: int
    n_observations: int
    unsupported: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _to_python(value: Any) -> Any:
    """把 numpy 标量/数组转成原生 Python 类型（供 json 序列化）。"""
    if isinstance(value, np.ndarray):
        return [_to_python(v) for v in value.tolist()]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _get_attr(var: netCDF4.Variable, name: str) -> Any | None:
    return var.getncattr(name) if name in var.ncattrs() else None


def _get_fill_markers(var: netCDF4.Variable) -> list[Any]:
    """收集变量自身的缺测标记，顺序：_FillValue 然后 missing_value。

    以文件中的存储值为准（不启用 netCDF4 自动 mask/scale）。
    """
    markers: list[Any] = []
    fill = _get_attr(var, "_FillValue")
    if fill is not None:
        markers.append(np.asarray(fill, dtype=var.dtype).item())
    missing = _get_attr(var, "missing_value")
    if missing is not None:
        arr = np.atleast_1d(np.asarray(missing))
        for m in arr:
            markers.append(np.asarray(m, dtype=var.dtype).item())
    return markers


def _is_string_var(var: netCDF4.Variable) -> bool:
    # VLEN 字符串变量的 dtype 直接是 Python str；普通 char/定长字符为 numpy 类型
    if var.dtype is str:
        return True
    kind = getattr(var.dtype, "kind", None)
    return kind in {"U", "S", "T"}


def _read_strings(var: netCDF4.Variable) -> list[str]:
    """读取字符/字符串变量并统一成 Python str 列表。"""
    raw = var[:]
    if np.isscalar(raw) or (isinstance(raw, np.ndarray) and raw.ndim == 0):
        return [str(np.asarray(raw).item())]
    return [str(x) for x in np.asarray(raw).tolist()]


def _read_scalar_string(var: netCDF4.Variable) -> str:
    return str(np.asarray(var[:]).item())


# ---------------------------------------------------------------------------
# 校验辅助：索引/计数变量
# ---------------------------------------------------------------------------

def _require_var(ds: netCDF4.Dataset, name: str, source: str,
                 expect_dim: str | None = None,
                 attr_check: tuple[str, str] | None = None
                 ) -> netCDF4.Variable:
    """确认变量存在、维度正确，并可选核对 (属性名, 期望值)。"""
    if name not in ds.variables:
        raise ValidationError(
            f"{source}: 缺少必需变量 '{name}'")
    var = ds.variables[name]
    if expect_dim is not None and var.dimensions != (expect_dim,):
        raise ValidationError(
            f"{source}: 变量 '{name}' 维度应为 ('{expect_dim}',)，"
            f"实际为 {var.dimensions}")
    if attr_check is not None:
        attr, expected = attr_check
        actual = _get_attr(var, attr)
        if actual != expected:
            raise ValidationError(
                f"{source}: 变量 '{name}' 的属性 {attr} 应为 "
                f"'{expected}'，实际为 {actual!r}")
    return var


def _read_index_values(var: netCDF4.Variable, source: str,
                       dim_size: int, target_name: str) -> list[int]:
    """读取整数索引变量：必须非缺失、非负且在 [0, dim_size) 内。"""
    if getattr(var.dtype, "kind", None) not in "iu":
        raise ValidationError(
            f"{source}: 索引变量 '{var.name}' 的 dtype 必须是整数，"
            f"实际为 {var.dtype}")
    raw = np.asarray(var[:])
    values = raw.tolist()
    for i, v in enumerate(values):
        # netCDF4 未开自动 mask，但仍显式拒绝 NaN/inf 与非整数浮点场景
        if isinstance(v, float) and not np.isfinite(v):
            raise ValidationError(
                f"{source}: 变量 '{var.name}' 索引 {i} 为缺失/非有限值 {v}")
        if v < 0 or v >= dim_size:
            raise ValidationError(
                f"{source}: 变量 '{var.name}' 索引 {i} 的值 {v} 越界，"
                f"目标维度 '{target_name}' 长度为 {dim_size}，"
                f"合法范围 [0, {dim_size - 1}]")
    return [int(v) for v in values]


def _read_row_sizes(var: netCDF4.Variable, source: str,
                    obs_size: int) -> list[int]:
    """读取 rowSize：整数、允许 0、必须非负，总和必须等于 obs 维度。"""
    if getattr(var.dtype, "kind", None) not in "iu":
        raise ValidationError(
            f"{source}: rowSize 变量 '{var.name}' 的 dtype 必须是整数，"
            f"实际为 {var.dtype}")
    values = [int(v) for v in np.asarray(var[:]).tolist()]
    for i, v in enumerate(values):
        if v < 0:
            raise ValidationError(
                f"{source}: 变量 '{var.name}' 索引 {i} 的计数 {v} 为负数，"
                f"rowSize 必须非负")
    total = sum(values)
    if total != obs_size:
        raise ValidationError(
            f"{source}: 变量 '{var.name}' 总和为 {total}，"
            f"与 sample_dimension 'obs' 长度 {obs_size} 不一致")
    return values


def _check_unique(ids: list[str], source: str, var_name: str) -> None:
    seen: dict[str, int] = {}
    for i, value in enumerate(ids):
        if value in seen:
            raise ValidationError(
                f"{source}: 变量 '{var_name}' 的业务 ID {value!r} 不唯一，"
                f"首次出现于索引 {seen[value]}，再次出现于索引 {i}")
        seen[value] = i


# ---------------------------------------------------------------------------
# 数值解码：先识别存储值的填充标记，再应用 scale_factor/add_offset
# ---------------------------------------------------------------------------

def _decode_values(var: netCDF4.Variable) -> list[Any]:
    """读取变量存储值并解码为关系表中的 JSON 值。

    顺序严格为：
      1. 取存储值（关闭自动 mask 与自动 scale）；
      2. 与变量自身的 _FillValue / missing_value（存储 dtype）比对，命中 -> None；
      3. 未命中者再应用 scale_factor / add_offset。
    数值 0、空字符串都不是缺测。
    """
    raw = np.asarray(var[:])
    flat = raw.reshape(-1)
    markers = _get_fill_markers(var)

    scale = _get_attr(var, "scale_factor")
    offset = _get_attr(var, "add_offset")
    scale = float(scale) if scale is not None else None
    offset = float(offset) if offset is not None else None

    out: list[Any] = []
    for item in flat:
        stored = item.item()
        is_fill = any(np.array_equal(stored, m) for m in markers)
        if is_fill:
            out.append(None)
            continue
        if scale is not None or offset is not None:
            value = float(stored)
            if scale is not None:
                value *= scale
            if offset is not None:
                value += offset
            out.append(value)
        else:
            out.append(_to_python(stored))
    return out


def _is_time_variable(var: netCDF4.Variable) -> bool:
    """本题口径：数值型、带 'X since Y' 单位的变量视为时间变量。"""
    kind = getattr(var.dtype, "kind", None)
    if kind is None or kind not in "iuf":
        return False
    units = _get_attr(var, "units")
    return isinstance(units, str) and " since " in units


def _decode_time(var: netCDF4.Variable, decoded: list[Any],
                 source: str) -> list[Any]:
    """把 seconds since + standard 日历的时间值转为 UTC ISO8601。

    缺测值 (None) 原样保留，输出位置与 obs/profile 索引一一对应。
    """
    units = _get_attr(var, "units")
    calendar = _get_attr(var, "calendar") or "standard"
    if not units.startswith("seconds since "):
        raise UnsupportedFeatureError(
            f"{source}: 时间变量 '{var.name}' 的单位 {units!r} 不受支持，"
            f"本题仅支持 'seconds since <参考点>'")
    if calendar not in SUPPORTED_CALENDARS:
        raise UnsupportedFeatureError(
            f"{source}: 时间变量 '{var.name}' 的日历 {calendar!r} 不受支持，"
            f"本题仅支持 standard/gregorian 日历")

    iso_values: list[Any] = []
    for value in decoded:
        if value is None:
            iso_values.append(None)
            continue
        dt = netCDF4.num2date(value, units=units, calendar=calendar)
        text = dt.isoformat()
        # 本题参考点均为 UTC：naive 结果或 +00:00 偏移统一成 ISO8601 的 Z
        offset = dt.utcoffset() if hasattr(dt, "utcoffset") else None
        if text.endswith("+00:00"):
            text = text[:-6] + "Z"
        elif offset is None or offset == timedelta(0):
            text = text + "Z"
        iso_values.append(text)
    return iso_values


# ---------------------------------------------------------------------------
# 变量分类与元数据
# ---------------------------------------------------------------------------

def _classify(var: netCDF4.Variable,
              dims: dict[str, str]) -> tuple[str | None, str | None]:
    """按变量维度归类为 scalar/trajectory/profile/observation。

    只接受标量或单维变量，且该维度必须是三层结构维度之一；
    其余情况显式标记 unsupported。返回 (level, unsupported_reason)。
    """
    if var.ndim == 0:
        return "scalar", None
    if var.ndim != 1:
        return None, f"维度数为 {var.ndim}，仅支持标量或单维变量"
    dim = var.dimensions[0]
    level = dims.get(dim)
    if level is None:
        return None, f"维度 '{dim}' 不属于 trajectory/profile/obs 结构维度"
    return level, None


def _var_metadata(name: str, var: netCDF4.Variable,
                  level: str | None, unsupported_reason: str | None,
                  structural: bool, is_time: bool) -> dict[str, Any]:
    units = _get_attr(var, "units")
    meta: dict[str, Any] = {
        "name": name,
        "dimensions": list(var.dimensions),
        "ndim": var.ndim,
        "dtype": str(var.dtype),
        "level": level,
        "table": _level_to_table(level),
        "structural": structural,
        "selected": not structural,
        "supported": unsupported_reason is None,
        "is_time": is_time,
        "units": units,
        "fill_value": _to_python(_get_attr(var, "_FillValue")),
        "missing_value": _to_python(_get_attr(var, "missing_value")),
        "scale_factor": _to_python(_get_attr(var, "scale_factor")),
        "add_offset": _to_python(_get_attr(var, "add_offset")),
        "calendar": _get_attr(var, "calendar"),
        "time_units": units if is_time else None,
        "long_name": _get_attr(var, "long_name"),
        "standard_name": _get_attr(var, "standard_name"),
        "attributes": {a: _to_python(var.getncattr(a))
                       for a in var.ncattrs()},
    }
    if unsupported_reason is not None:
        meta["unsupported_reason"] = unsupported_reason
    return meta


def _level_to_table(level: str | None) -> str | None:
    return {
        "scalar": OBSERVATION_TABLE,   # 标量经广播后只在观测继承视图落地
        "trajectory": TRAJECTORY_TABLE,
        "profile": PROFILE_TABLE,
        "observation": OBSERVATION_TABLE,
    }.get(level)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def convert_file(nc_path: str, mapping: dict[str, Any],
                 output_root: str) -> ConversionResult:
    """转换单个 NetCDF 文件，返回结果摘要。

    校验全部通过后才会在 output_root 下发布同名输出目录。
    """
    source = os.path.basename(nc_path)
    traj_dim = mapping["trajectory_dim"]
    prof_dim = mapping["profile_dim"]
    obs_dim = mapping["observation_dim"]
    traj_id_name = mapping["trajectory_id"]
    prof_id_name = mapping["profile_id"]
    p2t_name = mapping["profile_to_trajectory"]
    layout_cfg = mapping["observation_layout"]
    rowsize_name = layout_cfg[LAYOUT_CONTIGUOUS]
    profindex_name = layout_cfg[LAYOUT_INDEXED]
    selected_names = list(mapping["variables"])

    with netCDF4.Dataset(nc_path, "r") as ds:
        ds.set_auto_mask(False)
        ds.set_auto_scale(False)

        # --- 维度核对 -----------------------------------------------------
        for d in (traj_dim, prof_dim, obs_dim):
            if d not in ds.dimensions:
                raise ValidationError(
                    f"{source}: 缺少结构维度 '{d}'")
        n_traj = len(ds.dimensions[traj_dim])
        n_prof = len(ds.dimensions[prof_dim])
        n_obs = len(ds.dimensions[obs_dim])

        feature_type = ds.featureType if hasattr(ds, "featureType") else None
        if feature_type is not None and feature_type != "trajectoryProfile":
            raise ValidationError(
                f"{source}: featureType={feature_type!r}，"
                f"本转换器仅支持 trajectoryProfile")

        dims = {traj_dim: "trajectory", prof_dim: "profile",
                obs_dim: "observation"}

        # --- 结构变量核对（cf_role / instance_dimension / sample_dimension）
        traj_id_var = _require_var(
            ds, traj_id_name, source, traj_dim,
            ("cf_role", "trajectory_id"))
        prof_id_var = _require_var(
            ds, prof_id_name, source, prof_dim,
            ("cf_role", "profile_id"))
        p2t_var = _require_var(
            ds, p2t_name, source, prof_dim,
            ("instance_dimension", traj_dim))

        structural = {traj_id_name, prof_id_name, p2t_name,
                      rowsize_name, profindex_name}

        has_rowsize = rowsize_name in ds.variables
        has_profindex = profindex_name in ds.variables
        if has_rowsize and has_profindex:
            raise ValidationError(
                f"{source}: '{rowsize_name}' 与 '{profindex_name}' 同时存在，"
                f"contiguous 与 indexed 两种观测布局互斥")
        if not has_rowsize and not has_profindex:
            raise ValidationError(
                f"{source}: 观测布局变量均缺失，需要 '{rowsize_name}' "
                f"(contiguous) 或 '{profindex_name}' (indexed) 之一")

        if has_rowsize:
            layout = LAYOUT_CONTIGUOUS
            rs_var = _require_var(
                ds, rowsize_name, source, prof_dim,
                ("sample_dimension", obs_dim))
            row_sizes = _read_row_sizes(rs_var, source, n_obs)
            prof_index_per_obs: list[int] | None = None
        else:
            layout = LAYOUT_INDEXED
            pi_var = _require_var(
                ds, profindex_name, source, obs_dim,
                ("instance_dimension", prof_dim))
            prof_index_per_obs = _read_index_values(
                pi_var, source, n_prof, prof_dim)
            row_sizes = None

        # --- ID 与归属索引 ------------------------------------------------
        if not _is_string_var(traj_id_var):
            raise ValidationError(
                f"{source}: 轨迹 ID 变量 '{traj_id_name}' 必须是字符串类型")
        if not _is_string_var(prof_id_var):
            raise ValidationError(
                f"{source}: 剖面 ID 变量 '{prof_id_name}' 必须是字符串类型")
        traj_ids = _read_strings(traj_id_var)
        prof_ids = _read_strings(prof_id_var)
        _check_unique(traj_ids, source, traj_id_name)
        _check_unique(prof_ids, source, prof_id_name)

        traj_index_per_prof = _read_index_values(
            p2t_var, source, n_traj, traj_dim)

        # 每个 profile 拥有的 obs 索引（不展平 zip，按维度解释分段）
        obs_indices_by_profile: list[list[int]] = [[] for _ in range(n_prof)]
        if layout == LAYOUT_CONTIGUOUS:
            cursor = 0
            for p, size in enumerate(row_sizes):
                obs_indices_by_profile[p] = list(
                    range(cursor, cursor + size))
                cursor += size
        else:
            for o, p in enumerate(prof_index_per_obs):
                obs_indices_by_profile[p].append(o)
        obs_count = [len(x) for x in obs_indices_by_profile]

        # --- 选中变量存在性、结构变量冲突检查 -----------------------------
        for name in selected_names:
            if name not in ds.variables:
                raise ValidationError(
                    f"{source}: mapping 选中的变量 '{name}' 在文件中不存在")
            if name in structural:
                raise ValidationError(
                    f"{source}: 变量 '{name}' 是结构变量，"
                    f"不能作为数据变量列入 mapping.variables")

        # --- 全量变量盘点 + 分类 ------------------------------------------
        metadata: list[dict[str, Any]] = []
        unsupported_meta: list[dict[str, Any]] = []
        selected_by_level: dict[str, list[str]] = {
            "scalar": [], "trajectory": [],
            "profile": [], "observation": [],
        }
        selected_set = set(selected_names)
        for name, var in ds.variables.items():
            level, reason = _classify(var, dims)
            # 维度合法但 dtype 不支持（非数值、非字符串），同样显式 unsupported
            if reason is None and not _is_string_var(var) \
                    and getattr(var.dtype, "kind", None) not in "iuf":
                reason = (f"dtype {var.dtype} 不受支持，"
                          f"仅支持数值与字符串类型")
                level = None
            is_time = _is_time_variable(var)
            is_structural = name in structural
            meta = _var_metadata(name, var, level, reason,
                                 is_structural, is_time)
            meta["selected"] = is_structural or name in selected_set
            metadata.append(meta)
            if is_structural or reason is not None:
                if reason is not None:
                    unsupported_meta.append(meta)
                continue
            if name in selected_set:
                selected_by_level[level].append(name)

        # --- 解码选中变量 -------------------------------------------------
        decoded: dict[str, list[Any]] = {}
        time_iso: dict[str, list[Any]] = {}
        for name in selected_names:
            var = ds.variables[name]
            if any(m["name"] == name for m in unsupported_meta):
                continue
            if _is_string_var(var):
                if var.ndim == 0:
                    decoded[name] = [_read_scalar_string(var)]
                else:
                    decoded[name] = _read_strings(var)
            else:
                decoded[name] = _decode_values(var)
                if _is_time_variable(var):
                    time_iso[name] = _decode_time(
                        var, decoded[name], source)

        # --- 组装关系行（快照，校验已全部完成） ---------------------------
        traj_rows, prof_rows, obs_rows = _build_rows(
            source=source,
            layout=layout,
            n_traj=n_traj, n_prof=n_prof,
            traj_dim=traj_dim, prof_dim=prof_dim,
            traj_ids=traj_ids, prof_ids=prof_ids,
            traj_index_per_prof=traj_index_per_prof,
            row_sizes=row_sizes,
            prof_index_per_obs=prof_index_per_obs,
            obs_indices_by_profile=obs_indices_by_profile,
            obs_count=obs_count,
            selected_by_level=selected_by_level,
            decoded=decoded,
            time_iso=time_iso,
        )

        variables_doc = {
            "source_file": source,
            "feature_type": feature_type,
            "layout": layout,
            "dimensions": {
                traj_dim: n_traj, prof_dim: n_prof, obs_dim: n_obs,
            },
            "tables": {
                TRAJECTORY_TABLE: selected_by_level["trajectory"],
                PROFILE_TABLE: selected_by_level["profile"],
                OBSERVATION_TABLE: {
                    "own": selected_by_level["observation"],
                    "inherited_scalar": selected_by_level["scalar"],
                    "inherited_profile": selected_by_level["profile"],
                    "inherited_trajectory": selected_by_level["trajectory"],
                },
            },
            "variables": metadata,
            "unsupported": unsupported_meta,
        }

    # --- 原子发布：先写临时目录，再整体替换 -------------------------------
    output_dir = os.path.join(output_root, os.path.splitext(source)[0])
    _publish(output_dir, {
        "trajectory.jsonl": traj_rows,
        "profile.jsonl": prof_rows,
        "observation.jsonl": obs_rows,
        "variables.json": variables_doc,
    })

    return ConversionResult(
        source=source,
        output_dir=output_dir,
        layout=layout,
        n_trajectories=n_traj,
        n_profiles=n_prof,
        n_observations=n_obs,
        unsupported=unsupported_meta,
    )


# ---------------------------------------------------------------------------
# 关系行组装
# ---------------------------------------------------------------------------

def _build_rows(*, source: str, layout: str,
                n_traj: int, n_prof: int,
                traj_dim: str, prof_dim: str,
                traj_ids: list[str], prof_ids: list[str],
                traj_index_per_prof: list[int],
                row_sizes: list[int] | None,
                prof_index_per_obs: list[int] | None,
                obs_indices_by_profile: list[list[int]],
                obs_count: list[int],
                selected_by_level: dict[str, list[str]],
                decoded: dict[str, list[Any]],
                time_iso: dict[str, list[Any]]) -> tuple[list, list, list]:

    scalar_names = selected_by_level["scalar"]
    traj_names = selected_by_level["trajectory"]
    prof_names = selected_by_level["profile"]
    obs_names = selected_by_level["observation"]

    # 标量变量在文件中只有一个存储值，由索引链向每条观测广播
    scalar_values = {name: decoded[name][0] for name in scalar_names}

    # --- trajectory 表 ----------------------------------------------------
    traj_rows: list[dict[str, Any]] = []
    for t in range(n_traj):
        row: dict[str, Any] = {
            "trajectory_index": t,
            "trajectory_id": traj_ids[t],
            "variables": {},
        }
        for name in traj_names:
            row["variables"][name] = decoded[name][t]
            if name in time_iso:
                row["variables"][f"{name}_utc"] = time_iso[name][t]
        traj_rows.append(row)

    # --- profile 表：保留原始索引/profile_id，并关联 trajectory_id --------
    prof_rows: list[dict[str, Any]] = []
    for p in range(n_prof):
        t = traj_index_per_prof[p]
        row = {
            "profile_index": p,
            "profile_id": prof_ids[p],
            "trajectory_index": t,
            "trajectory_id": traj_ids[t],
            "obs_count": obs_count[p],
            "variables": {},
        }
        if layout == LAYOUT_CONTIGUOUS:
            row["row_size"] = row_sizes[p]
        prof_rows.append(row)
        for name in prof_names:
            row["variables"][name] = decoded[name][p]
            if name in time_iso:
                row["variables"][f"{name}_utc"] = time_iso[name][p]

    # --- observation 表：按原 obs 索引顺序，含完整继承视图 -----------------
    obs_rows: list[dict[str, Any]] = []
    for p in range(n_prof):
        t = traj_index_per_prof[p]
        # 继承值在 profile 循环内取一次，保证该 profile 下所有 obs 同源，
        # 绝不按展平后的行号 zip 猜测归属
        inherited_profile = {
            name: decoded[name][p] for name in prof_names
        }
        inherited_profile_utc = {
            f"{name}_utc": time_iso[name][p]
            for name in prof_names if name in time_iso
        }
        inherited_trajectory = {
            name: decoded[name][t] for name in traj_names
        }
        inherited_trajectory_utc = {
            f"{name}_utc": time_iso[name][t]
            for name in traj_names if name in time_iso
        }
        for o in obs_indices_by_profile[p]:
            own: dict[str, Any] = {}
            for name in obs_names:
                own[name] = decoded[name][o]
                if name in time_iso:
                    own[f"{name}_utc"] = time_iso[name][o]
            obs_rows.append({
                "source_file": source,
                "observation_index": o,
                "profile_index": p,
                "profile_id": prof_ids[p],
                "trajectory_index": t,
                "trajectory_id": traj_ids[t],
                "variables": own,
                "inherited": {
                    "scalar": dict(scalar_values),
                    "profile": {**inherited_profile,
                                **inherited_profile_utc},
                    "trajectory": {**inherited_trajectory,
                                   **inherited_trajectory_utc},
                },
            })

    # 按源 obs 索引排序输出，使 indexed 的交错顺序可被直接核对
    obs_rows.sort(key=lambda r: r["observation_index"])
    return traj_rows, prof_rows, obs_rows


# ---------------------------------------------------------------------------
# 原子发布
# ---------------------------------------------------------------------------

def _publish(output_dir: str, files: dict[str, Any]) -> None:
    """把全部关系表写到临时目录后整体替换，失败不留半成品。"""
    parent = os.path.dirname(os.path.abspath(output_dir))
    os.makedirs(parent, exist_ok=True)
    tmp_dir = output_dir + ".tmp"
    if os.path.exists(tmp_dir):
        _remove_tree(tmp_dir)
    os.makedirs(tmp_dir)
    try:
        for name, payload in files.items():
            path = os.path.join(tmp_dir, name)
            if name.endswith(".jsonl"):
                with open(path, "w", encoding="utf-8") as fh:
                    for row in payload:
                        fh.write(json.dumps(
                            row, ensure_ascii=False, allow_nan=False))
                        fh.write("\n")
            else:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False,
                              indent=2, allow_nan=False)
                    fh.write("\n")
        if os.path.exists(output_dir):
            _remove_tree(output_dir)
        os.replace(tmp_dir, output_dir)
    except BaseException:
        _remove_tree(tmp_dir)
        raise


def _remove_tree(path: str) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)
