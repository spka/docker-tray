"""Update state, caching and worker orchestration without GTK dependencies.

Callbacks receive an opaque caller context. The desktop adapter supplies the
dispatcher and UI callbacks; headless clients can use immediate dispatch.
"""

import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import docker_tray_platform
from docker_tray_commands import (
    COMMAND_ERROR_DETAIL_MAX_CHARS,
    get_command_failure_detail,
    get_authorization_failure_detail,
)
from docker_tray_state import UpdateCheckState, UpdateOperationState, RemoteDigestCache
from docker_tray_updates import (
    AppUpdate,
    ImageUpdateCheckError,
    format_relative_time,
    is_checkable_image,
)

UPDATE_CHECK_INTERVAL_SECONDS = 3600
REMOTE_DIGEST_CACHE_SECONDS = 15 * 60
REMOTE_DIGEST_FAILURE_CACHE_SECONDS = 2 * 60
REMOTE_DIGEST_WORKERS = 4


def start_worker(target, *args):
    threading.Thread(target=target, args=args, daemon=True).start()


class UpdateService:
    def __init__(
        self,
        backend,
        *,
        executor_factory=None,
        start_background=None,
        dispatch=None,
        on_changed=None,
        notify=None,
        restart=None,
        close_dialog=None,
    ):
        self.backend = backend
        self.version = backend.version
        self.executor_factory = executor_factory or ThreadPoolExecutor
        self.start_background = start_background or start_worker
        self.dispatch = dispatch or (lambda callback, *args: callback(*args))
        self.on_changed = on_changed or (lambda context, notice_changed: None)
        self.notify = notify or (lambda context, message: None)
        self.restart = restart or (lambda context: None)
        self.close_dialog = close_dialog or (lambda: None)
        self.update_check_state = UpdateCheckState(
            app_update=AppUpdate(False),
            engine_update=docker_tray_platform.EngineUpdate(False),
        )
        self.operation_state = UpdateOperationState()
        self.remote_digest_cache = RemoteDigestCache()
        self.stopped = threading.Event()

    def close(self):
        """Wake the periodic checker when the application shuts down."""
        self.stopped.set()

    def get_cached_remote_config_digest(self, image, now=None):
        now = time.monotonic() if now is None else now
        with self.remote_digest_cache.lock:
            cached = self.remote_digest_cache.values.get(image)
            if cached is not None and cached[0] > now:
                if cached[2] is not None:
                    raise RuntimeError(cached[2])
                return cached[1]
        try:
            digest = self.backend.get_remote_config_digest(image)
        except Exception as error:
            with self.remote_digest_cache.lock:
                self.remote_digest_cache.values[image] = (
                    now + REMOTE_DIGEST_FAILURE_CACHE_SECONDS,
                    None,
                    str(error),
                )
            raise
        ttl = REMOTE_DIGEST_CACHE_SECONDS if digest else REMOTE_DIGEST_FAILURE_CACHE_SECONDS
        with self.remote_digest_cache.lock:
            self.remote_digest_cache.values[image] = (now + ttl, digest, None)
        return digest

    def get_remote_digest_outcome(self, image):
        try:
            digest = self.get_cached_remote_config_digest(image)
            if not digest:
                raise RuntimeError("Registry returned no matching image digest")
            return digest
        except Exception as error:
            return RuntimeError(str(error))

    def check_image_updates(self):
        container_refs = self.backend.get_container_image_refs()
        images = sorted(
            {image for image, _container_image_id in container_refs if is_checkable_image(image)}
        )
        local_metadata = self.backend.get_local_image_metadata(images)
        registry_images = [
            image
            for image in images
            if (metadata := local_metadata.get(image)) and metadata.registry_backed
        ]
        stale_running_images = {
            image
            for image, container_image_id in container_refs
            if image in registry_images
            and container_image_id
            and (local := local_metadata[image].image_id)
            and (local != container_image_id)
        }
        remote_ids = {}
        if registry_images:
            worker_count = min(REMOTE_DIGEST_WORKERS, len(registry_images))
            with self.executor_factory(max_workers=worker_count) as executor:
                remote_ids = dict(
                    zip(
                        registry_images,
                        executor.map(self.get_remote_digest_outcome, registry_images),
                    )
                )
        remote_updates = {
            image
            for image in registry_images
            if (local := local_metadata[image].image_id)
            and (remote := remote_ids.get(image))
            and (not isinstance(remote, Exception))
            and (local != remote)
        }
        updates = remote_updates | stale_running_images
        failures = {
            image: str(outcome)
            for image, outcome in remote_ids.items()
            if isinstance(outcome, Exception)
        }
        if failures:
            raise ImageUpdateCheckError(updates, failures)
        return sorted(updates)

    def get_update_state_snapshot(self):
        with self.update_check_state.lock:
            return (
                self.update_check_state.engine_update,
                list(self.update_check_state.image_updates),
            )

    def get_app_update_snapshot(self):
        with self.update_check_state.lock:
            return self.update_check_state.app_update

    def get_update_feedback_snapshot(self):
        with self.update_check_state.lock:
            return {
                "checking": self.update_check_state.checking,
                "last_checked": self.update_check_state.last_checked,
                "errors": tuple(self.update_check_state.errors),
            }

    def get_update_menu_notice(self, app_update, engine_update, image_updates, errors):
        if app_update.available or engine_update.available or image_updates:
            return "updates"
        if errors:
            return "error"
        return None

    def set_update_feedback(self, context, checking, last_checked=None, errors=None):
        with self.update_check_state.lock:
            previous_notice = self.get_update_menu_notice(
                self.update_check_state.app_update,
                self.update_check_state.engine_update,
                self.update_check_state.image_updates,
                self.update_check_state.errors,
            )
            changed = self.update_check_state.checking != checking
            self.update_check_state.checking = checking
            if last_checked is not None:
                changed = changed or self.update_check_state.last_checked != last_checked
                self.update_check_state.last_checked = last_checked
            if errors is not None:
                errors = tuple(errors)
                changed = changed or self.update_check_state.errors != errors
                self.update_check_state.errors = errors
            notice_changed = previous_notice != self.get_update_menu_notice(
                self.update_check_state.app_update,
                self.update_check_state.engine_update,
                self.update_check_state.image_updates,
                self.update_check_state.errors,
            )
        if changed:
            self.on_changed(context, notice_changed)
        return False

    def set_update_state(self, context, engine_update, image_updates, app_update=None):
        with self.update_check_state.lock:
            previous_notice = self.get_update_menu_notice(
                self.update_check_state.app_update,
                self.update_check_state.engine_update,
                self.update_check_state.image_updates,
                self.update_check_state.errors,
            )
            if app_update is None:
                app_update = self.update_check_state.app_update
            app_update_became_available = (
                app_update.available and app_update != self.update_check_state.app_update
            )
            changed = (
                app_update != self.update_check_state.app_update
                or engine_update != self.update_check_state.engine_update
                or image_updates != self.update_check_state.image_updates
            )
            self.update_check_state.app_update = app_update
            self.update_check_state.engine_update = engine_update
            self.update_check_state.image_updates = image_updates
            notice_changed = previous_notice != self.get_update_menu_notice(
                self.update_check_state.app_update,
                self.update_check_state.engine_update,
                self.update_check_state.image_updates,
                self.update_check_state.errors,
            )
        if changed:
            self.on_changed(context, notice_changed)
        if app_update_became_available:
            self.notify(context, f"Docker Tray {app_update.latest_version} is available.")
        return False

    def run_update_check(self, context):
        if self.stopped.is_set():
            return
        if not self.update_check_state.run_lock.acquire(blocking=False):
            return
        completion_started = False

        def finish(*args):
            nonlocal completion_started
            completion_started = True
            return self.finish_update_check(*args)

        try:
            self.dispatch(self.set_update_feedback, context, True)
            previous_engine_update, previous_image_updates = self.get_update_state_snapshot()
            previous_app_update = self.get_app_update_snapshot()
            errors = []
            try:
                app_update = self.backend.check_app_update()
            except Exception as error:
                app_update = previous_app_update
                errors.append(
                    f"Docker Tray release check: {get_command_failure_detail(error=error)}"
                )
            try:
                engine_update = self.backend.check_engine_update()
            except Exception as error:
                engine_update = previous_engine_update
                errors.append(f"Docker Engine check: {get_command_failure_detail(error=error)}")
            try:
                image_updates = self.check_image_updates()
            except ImageUpdateCheckError as error:
                image_updates = sorted(
                    set(error.updates)
                    | {image for image in previous_image_updates if image in error.failures}
                )
                errors.extend(
                    (
                        f"Image registry check: {image}: {detail}"
                        for image, detail in error.failures.items()
                    )
                )
            except Exception as error:
                image_updates = previous_image_updates
                errors.append(f"Image registry check: {get_command_failure_detail(error=error)}")
            self.dispatch(
                finish,
                context,
                engine_update,
                image_updates,
                app_update,
                time.time(),
                errors,
            )
        except BaseException:
            if not completion_started:
                self.update_check_state.run_lock.release()
            raise

    def finish_update_check(
        self, context, engine_update, image_updates, app_update, checked_at, errors
    ):
        """Keep scans serialized until their state reaches the UI dispatcher."""
        try:
            self.set_update_state(context, engine_update, image_updates, app_update)
            self.set_update_feedback(context, False, checked_at, errors)
        finally:
            self.update_check_state.run_lock.release()
        return False

    def start_update_check(self, context):
        self.start_background(self.run_update_check, context)

    def get_update_check_label(self, _item=None):
        feedback = self.get_update_feedback_snapshot()
        if feedback["checking"]:
            return "Checking for updates…"
        if feedback["errors"]:
            return f"Update check incomplete ({format_relative_time(feedback['last_checked'])})"
        if feedback["last_checked"] is None:
            return "Updates not checked yet"
        return f"Updates checked {format_relative_time(feedback['last_checked'])}"

    def poll_updates(self, context):
        self.run_update_check(context)
        while not self.stopped.wait(UPDATE_CHECK_INTERVAL_SECONDS):
            self.run_update_check(context)

    def start_app_upgrade(self, context):
        if (
            self.operation_state.app_upgrading
            or self.operation_state.engine_upgrading
            or self.operation_state.pulling_images
        ):
            return
        app_update = self.get_app_update_snapshot()
        if not app_update.can_install:
            return
        self.operation_state.app_upgrading = True
        self.operation_state.status = f"Downloading Docker Tray {app_update.latest_version}..."
        self.on_changed(context, False)

        def _upgrade():
            try:
                result = self.backend.run_app_upgrade(app_update)
            except Exception as error:
                self.dispatch(self.finish_app_upgrade, context, None, error)
                return
            self.dispatch(self.finish_app_upgrade, context, result, None)

        self.start_background(_upgrade)

    def finish_app_upgrade(self, context, result, error):
        self.operation_state.app_upgrading = False
        if error is not None:
            if isinstance(error, subprocess.TimeoutExpired):
                detail = "timed out"
            else:
                detail = f"{type(error).__name__}: {error}"
            self.operation_state.status = f"Docker Tray upgrade failed: {detail}"
            self.on_changed(context, False)
            return False
        if result.returncode != 0:
            detail = get_authorization_failure_detail(result, "upgrade command failed")
            self.operation_state.status = f"Docker Tray upgrade failed: {detail}"
            self.on_changed(context, False)
            return False
        self.operation_state.status = "Docker Tray upgraded. Restarting..."
        self.restart(context)
        return False

    def start_docker_engine_upgrade(self, context):
        if (
            self.operation_state.app_upgrading
            or self.operation_state.engine_upgrading
            or self.operation_state.pulling_images
        ):
            return
        self.operation_state.engine_upgrading = True
        self.operation_state.status = "Upgrading Docker Engine..."
        self.on_changed(context, False)

        def _upgrade():
            engine_update, _image_updates = self.get_update_state_snapshot()
            try:
                result = self.backend.run_engine_upgrade(engine_update)
            except Exception as error:
                self.dispatch(self.finish_docker_engine_upgrade, context, None, error)
                return
            self.dispatch(self.finish_docker_engine_upgrade, context, result, None)

        self.start_background(_upgrade)

    def finish_docker_engine_upgrade(self, context, result, error):
        self.operation_state.engine_upgrading = False
        if error is not None:
            if isinstance(error, subprocess.TimeoutExpired):
                detail = "timed out"
            else:
                detail = f"{type(error).__name__}: {error}"
            self.operation_state.status = f"Docker Engine upgrade failed: {detail}"
            self.on_changed(context, False)
            return False
        if result.returncode == 0:
            self.operation_state.status = ""
            _current_engine_update, image_updates = self.get_update_state_snapshot()
            self.set_update_state(context, docker_tray_platform.EngineUpdate(False), image_updates)
            self.close_dialog()
            self.start_background(self.run_update_check, context)
        else:
            detail = get_authorization_failure_detail(result, "upgrade command failed")
            self.operation_state.status = f"Docker Engine upgrade failed: {detail}"
            self.on_changed(context, False)
        return False

    def finish_image_pull(
        self,
        context,
        image,
        service_count,
        removed_image_count=0,
        cleanup_error="",
        start_recheck=True,
    ):
        self.operation_state.pulling_images.discard(image)
        engine_update, image_updates = self.get_update_state_snapshot()
        image_updates = [update_image for update_image in image_updates if update_image != image]
        self.set_update_state(context, engine_update, image_updates)
        status = f"Finished pulling and restarting {image} for {service_count} compose service(s)."
        if removed_image_count:
            noun = "image" if removed_image_count == 1 else "images"
            status += f" Removed {removed_image_count} replaced {noun}."
        if cleanup_error:
            status += f" Replaced image cleanup failed: {cleanup_error}"
        self.operation_state.status = status
        self.on_changed(context, False)
        if start_recheck:
            self.start_background(self.run_update_check, context)
        return False

    def set_image_pull_status(self, context, status):
        self.operation_state.status = status
        self.on_changed(context, False)
        return False

    def fail_image_pull(self, context, image, status):
        self.operation_state.pulling_images.discard(image)
        self.operation_state.status = status
        self.on_changed(context, False)
        return False

    def schedule_image_pull_failure(self, context, image, status, schedule_completion=True):
        if schedule_completion:
            self.dispatch(self.fail_image_pull, context, image, status)
        return (False, status, 0, "")

    def run_image_compose_pull_safely(
        self, context, image, start_recheck=True, schedule_completion=True
    ):
        try:
            return self.run_image_compose_pull(
                context, image, start_recheck=start_recheck, schedule_completion=schedule_completion
            )
        except Exception as error:
            status = f"Unexpected failure while updating {image}: {type(error).__name__}: {error}"
            return self.schedule_image_pull_failure(
                context, image, status, schedule_completion=schedule_completion
            )

    def start_image_compose_pull(self, button, context, image):
        if (
            self.operation_state.app_upgrading
            or self.operation_state.engine_upgrading
            or self.operation_state.pulling_images
        ):
            return
        self.operation_state.status = f"Pulling {image}..."
        self.operation_state.pulling_images.add(image)
        self.on_changed(context, False)

        def _pull():
            self.run_image_compose_pull_safely(context, image)

        self.start_background(_pull)

    def finish_all_image_pulls(
        self, context, images, successful_images, removed_image_count, errors, cleanup_errors
    ):
        total_count = len(images)
        success_count = len(successful_images)
        self.operation_state.pulling_images.clear()
        engine_update, image_updates = self.get_update_state_snapshot()
        successful_images = set(successful_images)
        remaining_updates = [image for image in image_updates if image not in successful_images]
        self.set_update_state(context, engine_update, remaining_updates)
        if errors:
            detail = "; ".join(errors)
            if len(detail) > COMMAND_ERROR_DETAIL_MAX_CHARS:
                detail = detail[:COMMAND_ERROR_DETAIL_MAX_CHARS].rstrip() + "…"
            self.operation_state.status = f"Batch finished: {success_count} of {total_count} images updated. Failures: {detail}"
        else:
            image_noun = "image" if total_count == 1 else "images"
            self.operation_state.status = f"Updated and cleaned up all {total_count} {image_noun}."
            if removed_image_count:
                removed_noun = "image" if removed_image_count == 1 else "images"
                self.operation_state.status += (
                    f" Removed {removed_image_count} replaced {removed_noun}."
                )
        if cleanup_errors:
            cleanup_detail = "; ".join(cleanup_errors)
            if len(cleanup_detail) > COMMAND_ERROR_DETAIL_MAX_CHARS:
                cleanup_detail = cleanup_detail[:COMMAND_ERROR_DETAIL_MAX_CHARS].rstrip() + "…"
            self.operation_state.status += f" Cleanup warnings: {cleanup_detail}"
        self.on_changed(context, False)
        self.start_background(self.run_update_check, context)
        return False

    def start_all_image_compose_pulls(self, context):
        if (
            self.operation_state.app_upgrading
            or self.operation_state.engine_upgrading
            or self.operation_state.pulling_images
        ):
            return
        _engine_update, image_updates = self.get_update_state_snapshot()
        if not image_updates:
            return
        images = list(image_updates)
        self.operation_state.pulling_images.update(images)
        self.operation_state.status = f"Updating 1 of {len(images)} images..."
        self.on_changed(context, False)

        def _pull_all():
            removed_image_count = 0
            errors = []
            cleanup_errors = []
            successful_images = []
            try:
                outcomes = self.backend.run_privileged_image_updates(images)
            except Exception as error:
                detail = get_command_failure_detail(error=error)
                outcomes = {
                    image: {
                        "success": False,
                        "error": detail,
                        "removed_image_count": 0,
                        "cleanup_error": "",
                    }
                    for image in images
                }
            for index, image in enumerate(images, start=1):
                self.dispatch(
                    self.set_image_pull_status,
                    context,
                    f"Updating {index} of {len(images)}: {image}...",
                )
                outcome = outcomes[image]
                if outcome["success"]:
                    successful_images.append(image)
                    removed_image_count += outcome["removed_image_count"]
                    if outcome["cleanup_error"]:
                        cleanup_errors.append(f"{image}: {outcome['cleanup_error']}")
                else:
                    errors.append(f"{image}: {outcome['error']}")
            self.dispatch(
                self.finish_all_image_pulls,
                context,
                images,
                successful_images,
                removed_image_count,
                errors,
                cleanup_errors,
            )

        self.start_background(_pull_all)

    def run_image_compose_pull(self, context, image, start_recheck=True, schedule_completion=True):
        outcome = self.backend.run_privileged_image_updates([image])[image]
        if not outcome["success"]:
            return self.schedule_image_pull_failure(
                context,
                image,
                f"Update failed for {image}: {outcome['error']}",
                schedule_completion=schedule_completion,
            )
        if schedule_completion:
            self.dispatch(
                self.finish_image_pull,
                context,
                image,
                outcome["service_count"],
                outcome["removed_image_count"],
                outcome["cleanup_error"],
                start_recheck,
            )
        return (True, "", outcome["removed_image_count"], outcome["cleanup_error"])
