import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Optional

import psutil

from apex_spider import main as run_apex_spider
from augment_icon_refresh import (
    is_augment_icon_prefetch_ready,
    manifest_has_incomplete_entries,
    run_augment_icon_prefetch,
)
from hero_sync import AUGMENT_ICON_FILE, AUGMENT_MAP_FILE, CONFIG_DIR, CORE_DATA_FILE, sync_hero_data
from hextech_query import get_latest_csv
from hextech_scraper import main_scraper
from log_utils import log_task_summary
from precomputed_api_cache import (
    load_precomputed_champion_list,
    has_precomputed_hextech_cache,
    rebuild_precomputed_api_cache_from_latest_csv,
)

logger = logging.getLogger(__name__)

_refresh_lock = threading.Lock()
_state_lock = threading.Lock()
_scheduled_lock = threading.Lock()
_refresh_lock_file = os.path.join(CONFIG_DIR, "backend_refresh.lock")
_startup_status_file = os.path.join(CONFIG_DIR, "startup_status.json")
_synergy_file = os.path.join(CONFIG_DIR, "Champion_Synergy.json")
_synergy_stale_after = 24 * 3600
_stale_lock_after = 15 * 60
_scheduled_refresh_thread: Optional[threading.Thread] = None


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _default_status() -> dict:
    return {
        "first_run": False,
        "hero_ready": False,
        "hextech_ready": False,
        "synergy_ready": False,
        "augment_icons_prefetched": False,
        "api_cache_ready": False,
        "in_progress_tasks": [],
        "last_error": "",
        "updated_at": _now_iso(),
    }


def _write_status_file(status: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    payload = dict(_default_status())
    payload.update(status)
    payload["updated_at"] = _now_iso()
    tmp_path = _startup_status_file + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, _startup_status_file)


def get_startup_status() -> dict:
    try:
        with open(_startup_status_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, dict):
            merged = _default_status()
            merged.update(payload)
            return merged
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return _default_status()


def _update_status(**updates) -> dict:
    with _state_lock:
        payload = get_startup_status()
        payload.update(updates)
        _write_status_file(payload)
        return payload


def _set_task_state(task_name: str, running: bool) -> None:
    with _state_lock:
        payload = get_startup_status()
        tasks = list(payload.get("in_progress_tasks", []))
        if running:
            if task_name not in tasks:
                tasks.append(task_name)
        else:
            tasks = [item for item in tasks if item != task_name]
        payload["in_progress_tasks"] = tasks
        _write_status_file(payload)
def _is_first_run(force: bool = False) -> bool:
    if force:
        return True

    core_files_ready = all(
        os.path.exists(path)
        for path in (CORE_DATA_FILE, AUGMENT_MAP_FILE, AUGMENT_ICON_FILE, _synergy_file)
    )
    latest_csv = get_latest_csv()
    return not core_files_ready or not latest_csv or not os.path.exists(latest_csv)


def _read_lock_payload() -> tuple[Optional[int], Optional[float]]:
    try:
        with open(_refresh_lock_file, "r", encoding="utf-8") as f:
            raw = f.read().strip().split()
        if len(raw) >= 2:
            return int(raw[0]), float(raw[1])
        if len(raw) == 1:
            return int(raw[0]), None
    except (OSError, ValueError, TypeError):
        pass
    return None, None


def _pid_is_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        return psutil.pid_exists(pid)
    except Exception:
        return False


def _acquire_file_lock() -> Optional[int]:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    while True:
        try:
            fd = os.open(_refresh_lock_file, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, f"{os.getpid()} {time.time()}".encode("utf-8"))
            return fd
        except FileExistsError:
            lock_pid, lock_ts = _read_lock_payload()
            try:
                lock_age = time.time() - (lock_ts or os.path.getmtime(_refresh_lock_file))
                if lock_pid and not _pid_is_alive(lock_pid):
                    os.remove(_refresh_lock_file)
                    continue
                if lock_age > _stale_lock_after:
                    os.remove(_refresh_lock_file)
                    continue
            except OSError:
                pass
            return None


def _release_file_lock(fd: int) -> None:
    try:
        os.close(fd)
    finally:
        try:
            os.remove(_refresh_lock_file)
        except OSError:
            pass


def _should_refresh_synergy(force: bool) -> bool:
    if force or not os.path.exists(_synergy_file):
        return True
    try:
        return (time.time() - os.path.getmtime(_synergy_file)) > _synergy_stale_after
    except OSError:
        return True


def _stop_requested(stop_event) -> bool:
    return bool(stop_event is not None and stop_event.is_set())


def _run_synergy_task(stop_event=None) -> bool:
    _set_task_state("synergy", True)
    try:
        if _stop_requested(stop_event):
            return False
        run_apex_spider()
        ok = os.path.exists(_synergy_file)
        _update_status(synergy_ready=ok)
        return ok
    except Exception as e:
        logger.exception("Champion_Synergy.json 刷新失败。")
        _update_status(last_error=f"synergy: {e}")
        return False
    finally:
        _set_task_state("synergy", False)


def _run_hextech_task(stop_event=None) -> bool:
    _set_task_state("hextech", True)
    try:
        if _stop_requested(stop_event):
            return False
        ok = bool(main_scraper(stop_event))
        if not ok:
            latest_csv = get_latest_csv()
            ok = bool(latest_csv and os.path.exists(latest_csv))
        _update_status(hextech_ready=ok)
        return ok
    except Exception as e:
        logger.exception("Hextech CSV 刷新失败。")
        _update_status(last_error=f"hextech: {e}")
        return False
    finally:
        _set_task_state("hextech", False)


def _run_augment_icon_task(force_refresh: bool, stop_event=None) -> bool:
    started_at = time.time()
    _set_task_state("augment_icons", True)
    try:
        result = run_augment_icon_prefetch(
            force_refresh=force_refresh,
            stop_event=stop_event,
            max_workers=8,
        )
        ready = bool(result.get("ready"))
        _update_status(augment_icons_prefetched=ready)
        log_task_summary(
            logger,
            task="海克斯图标预缓存",
            started_at=started_at,
            success=ready,
            detail=f"success={result['success']} failed={result['failed']} mode={result.get('mode')}",
        )
        return ready
    except Exception as e:
        logger.exception("海克斯图标批量预缓存失败。")
        _update_status(last_error=f"augment_icons: {e}")
        return False
    finally:
        _set_task_state("augment_icons", False)


def refresh_backend_data(force: bool = False, stop_event=None) -> bool:
    # 刷新桌面界面和网页层共享的运行数据。
    started_at = time.time()
    with _refresh_lock:
        lock_fd = _acquire_file_lock()
        if lock_fd is None:
            logger.info("后台刷新跳过：已有进行中的任务")
            return False

        first_run = _is_first_run(force=force)
        try:
            api_cache_ready = bool(load_precomputed_champion_list()) and has_precomputed_hextech_cache()
            _write_status_file({
                "first_run": first_run,
                "hero_ready": False,
                "hextech_ready": bool(get_latest_csv()),
                "synergy_ready": os.path.exists(_synergy_file),
                "augment_icons_prefetched": is_augment_icon_prefetch_ready(),
                "api_cache_ready": api_cache_ready,
                "in_progress_tasks": ["hero_sync"],
                "last_error": "",
            })

            hero_ok = sync_hero_data()
            _update_status(hero_ready=hero_ok)
            _set_task_state("hero_sync", False)
            if not hero_ok:
                _update_status(last_error="hero_sync failed")
                return False

            if _stop_requested(stop_event):
                logger.warning("后台刷新失败：stage=hero_sync interrupted=true")
                return False

            synergy_needed = _should_refresh_synergy(force or first_run)
            augment_needed = first_run or force or manifest_has_incomplete_entries()
            hextech_needed = force or first_run or not bool(get_latest_csv())

            if synergy_needed:
                threading.Thread(
                    target=_run_synergy_task,
                    args=(stop_event,),
                    daemon=True,
                    name="synergy-prefetch",
                ).start()
            else:
                _update_status(synergy_ready=os.path.exists(_synergy_file))

            if augment_needed:
                threading.Thread(
                    target=_run_augment_icon_task,
                    args=(True, stop_event),
                    daemon=True,
                    name="augment-icon-prefetch",
                ).start()
            else:
                _update_status(augment_icons_prefetched=is_augment_icon_prefetch_ready())

            hextech_result = True
            if hextech_needed:
                hextech_result = bool(_run_hextech_task(stop_event))
            else:
                _update_status(hextech_ready=bool(get_latest_csv()))

            latest_csv = get_latest_csv()
            api_cache_ready = bool(load_precomputed_champion_list()) and has_precomputed_hextech_cache()
            if latest_csv and os.path.exists(latest_csv) and (force or not api_cache_ready):
                api_cache_ready = bool(rebuild_precomputed_api_cache_from_latest_csv())
            _update_status(api_cache_ready=api_cache_ready)
            final_status = get_startup_status()
            refresh_ok = bool(hero_ok and (hextech_result or bool(latest_csv and os.path.exists(latest_csv))))
            log_task_summary(
                logger,
                task="后台刷新",
                started_at=started_at,
                success=refresh_ok,
                detail=(
                    f"hero_sync={hero_ok} "
                    f"hextech={hextech_result and bool(latest_csv and os.path.exists(latest_csv))} "
                    f"synergy={final_status.get('synergy_ready')} "
                    f"augment_icons={final_status.get('augment_icons_prefetched')}"
                ),
            )
            return refresh_ok
        finally:
            _release_file_lock(lock_fd)


def schedule_backend_refresh(
    *,
    force: bool = False,
    stop_event=None,
    thread_name: str = "backend-refresh",
) -> bool:
    # 统一后台刷新入口，避免 UI、API 和生命周期重复起线程抢同一把锁。
    global _scheduled_refresh_thread

    with _scheduled_lock:
        if _scheduled_refresh_thread is not None and _scheduled_refresh_thread.is_alive():
            return False

        thread = threading.Thread(
            target=refresh_backend_data,
            kwargs={"force": force, "stop_event": stop_event},
            daemon=True,
            name=thread_name,
        )
        _scheduled_refresh_thread = thread
        thread.start()
        return True
