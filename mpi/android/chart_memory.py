import re

from mpi.memory_qa import oom_state, parse_processes, parse_start_ticks, read_kib

SOURCE = "android_proc_anon_rss_swap"

_MAX_COMPONENT_KIB = 1_073_741_824
_MAX_SAFE_INTEGER = (1 << 53) - 1


def parse_replay_memory(row, platform):
    """Decode typed CSV fields, retaining the marker even when the sample is blank."""
    if row.get("mem_memory_source") != SOURCE:
        return {}
    result = {"memory_source": SOURCE}
    if platform != "android":
        return result

    def whole(key, lo, hi):
        value = row.get("mem_" + key)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,16}", value):
            return None
        number = int(value)
        return number if lo <= number <= hi else None

    pid = whole("play_memory_pid", 1, (1 << 31) - 1)
    ticks = whole("play_memory_start_ticks", 1, _MAX_SAFE_INTEGER)
    rss = whole("anon_rss_kib", 0, _MAX_COMPONENT_KIB)
    swap = whole("swap_kib", 0, _MAX_COMPONENT_KIB)
    combined = whole("anon_rss_plus_swap_kib", 0, 2 * _MAX_COMPONENT_KIB)
    if None in (pid, ticks, rss, swap, combined) or rss + swap != combined:
        return result

    state = row.get("mem_play_memory_state")
    state_source = row.get("mem_play_memory_state_source")
    if state not in ("foreground", "user_perceived_services", "background", "cached") or state_source != "measured_oom_score_adj":
        state = state_source = None
    category = row.get("mem_play_memory_category")
    category_source = row.get("mem_play_memory_category_source")
    if not ((category in ("app", "game") and category_source == "android_package_category_hint")
            or (category == "game" and category_source == "android_package_is_game_hint")):
        category = category_source = None
    form = row.get("mem_play_memory_form_factor")
    result.update(
        anon_rss_kib=rss, swap_kib=swap, anon_rss_plus_swap_kib=combined,
        play_memory_pid=pid, play_memory_start_ticks=ticks,
        play_memory_state=state, play_memory_state_source=state_source,
        play_memory_category=category, play_memory_category_source=category_source,
        play_memory_ram_kib=whole("play_memory_ram_kib", 1, _MAX_COMPONENT_KIB),
        play_memory_sdk_int=whole("play_memory_sdk_int", 21, 1000),
        play_memory_form_factor=form if form in ("phone_tablet", "other") else None,
    )
    return result


def read_capture_proc(raw, pid, package):
    """Exact selected process only; no memory from a reused PID."""
    parts = raw.split("\n_MPI_MEM_BOUNDARY_\n")
    if len(parts) != 8:
        return None, None, None, None
    stat_before, proc_before, oom_before, status, oom_after, proc_after, stat_after, tail = parts
    start = parse_start_ticks(stat_before, pid)
    if start is None or start != parse_start_ticks(stat_after, pid):
        return None, None, None, None
    if tail.strip() or any(not parse_processes([f"{pid} {name.strip()}"], package) for name in (proc_before, proc_after)) or proc_before != proc_after:
        return None, None, None, None
    def state(value):
        value = value.strip()
        return oom_state(int(value)) if value.isascii() and value.isdecimal() and len(value) <= 4 else "unknown"
    before, after = state(oom_before), state(oom_after)
    return read_kib(status, "RssAnon"), read_kib(status, "VmSwap"), before if oom_before.strip() == oom_after.strip() and before == after and before != "unknown" else None, start


def proc_command(pid):
    assert type(pid) is int and pid > 0
    base = f"/proc/{pid}"
    process = f"tr '\\000' '\\n' < {base}/cmdline | head -n 1"
    return "; printf '\\n_MPI_MEM_BOUNDARY_\\n'; ".join((
        f"cat {base}/stat", process, f"cat {base}/oom_score_adj",
        f"cat {base}/status", f"cat {base}/oom_score_adj", process,
        f"cat {base}/stat", "true",
    ))


def form_factor(characteristics, features=""):
    kinds = {value.strip() for value in characteristics.split(",")}
    features = {line.strip() for line in features.splitlines()}
    if features & {"feature:android.hardware.type.watch", "feature:android.hardware.type.television",
                   "feature:android.software.leanback", "feature:android.hardware.type.automotive",
                   "feature:android.hardware.type.embedded"}:
        return "other"
    if kinds & {"watch", "tv", "television", "automotive", "car", "embedded"}:
        return "other"
    if kinds & {"phone", "tablet"} or "feature:android.hardware.telephony" in features:
        return "phone_tablet"
    return "unknown"

# Package dumps vary by release/vendor. Trust only one exact Package block
# under Packages and positive, explicit platform metadata; absent is unknown.
def package_category(text, package):
    if "\nPackages:\n" not in "\n" + text:
        return None, None
    section = ("\n" + text).split("\nPackages:\n", 1)[1]
    block = None
    for line in section.splitlines():
        if line and not line[0].isspace():
            break
        header = re.fullmatch(r"  Package \[([^\]]+)\] \([^)]*\):", line)
        if header:
            if block is not None:
                break
            if header.group(1) == package:
                block = []
        elif block is not None:
            block.append(line.strip())
    if block is None:
        return None, None
    # ApplicationInfo categories 0 (game), 1..8 (defined non-game).
    categories = [m[1] for item in block if (m := re.fullmatch(r"(?:category|appCategory)=(-?[0-9]+)", item))]
    flag = any("IS_GAME" in m[1].split() for item in block if (m := re.fullmatch(r"(?:flags|pkgFlags)=\[([^\]]*)\]", item)))
    if len(categories) == 1 and categories[0] == "0":
        return "game", "android_package_category_hint"
    if len(categories) == 1 and categories[0] in set("12345678") and not flag:
        return "app", "android_package_category_hint"
    if not categories or categories == ["-1"]:
        if flag:
            return "game", "android_package_is_game_hint"
    return None, None
