from __future__ import annotations

import asyncio
import contextlib
import copy
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

from .meowtalk_service import (
    GroupConfig,
    GroupRuntime,
    build_group_configs,
    is_quiet_period,
    scheduled_due,
)


class MeowTalk(Star):
    """Send configurable corpus messages to subscribed group chats."""

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._group_configs: dict[str, GroupConfig] = {}
        self._group_states: dict[str, GroupRuntime] = {}
        self._config_snapshot: dict | None = None
        self._config_errors: tuple[str, ...] = ()
        self._scheduled_slots: dict[str, datetime] = {}
        self._timezone = timezone.utc

    async def initialize(self) -> None:
        """Start the background scheduler after the plugin is loaded."""
        self._timezone = self._resolve_timezone()
        self._refresh_group_configs(self._now())
        self._task = asyncio.create_task(
            self._scheduler_loop(),
            name="meowtalk-scheduler",
        )
        logger.info("MeowTalk scheduler started")

    async def terminate(self) -> None:
        """Stop the background scheduler when the plugin is unloaded."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("MeowTalk scheduler stopped")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """Record activity for a subscribed group and restart its idle timer."""
        self_id = event.get_self_id()
        if self_id and event.get_sender_id() == self_id:
            return
        now = self._now()
        async with self._lock:
            self._refresh_group_configs(now)
            group = self._group_configs.get(event.unified_msg_origin)
            if group is None or not group.enabled:
                return

            state = self._group_states.setdefault(
                group.umo,
                GroupRuntime(
                    quiet_active=is_quiet_period(group, now),
                    schedule_started_at=now,
                ),
            )
            if is_quiet_period(group, now):
                state.quiet_active = True
                state.idle_due = None
                state.idle_sent = False
                return

            state.quiet_active = False
            state.idle_sent = False
            if group.idle_enabled:
                jitter = random.uniform(
                    -group.idle_jitter_seconds,
                    group.idle_jitter_seconds,
                )
                state.idle_due = max(
                    now + timedelta(seconds=1),
                    now + timedelta(seconds=group.idle_timeout_seconds + jitter),
                )
            else:
                state.idle_due = None

    @filter.command_group("meowtalk")
    def meowtalk(self):
        """Manage MeowTalk subscriptions."""

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meowtalk.command("subscribe")
    async def subscribe(self, event: AstrMessageEvent):
        """Subscribe the current group using the plugin setting defaults."""
        if not event.get_group_id():
            yield event.plain_result("MeowTalk 只能在群聊中订阅。")
            return

        async with self._lock:
            groups = self.config.setdefault("groups", [])
            if not isinstance(groups, list):
                groups = []
                self.config["groups"] = groups
            umo = event.unified_msg_origin
            existing = next(
                (
                    item
                    for item in groups
                    if isinstance(item, dict) and item.get("umo") == umo
                ),
                None,
            )
            if existing is None:
                group_name = getattr(
                    getattr(event.message_obj, "group", None), "group_name", None
                )
                groups.append(
                    {
                        "__template_key": "group",
                        "enabled": True,
                        "group_name": group_name or event.get_group_id(),
                        "group_id": event.get_group_id(),
                        "umo": umo,
                        "idle_enabled": True,
                        "idle_timeout_minutes": 30.0,
                        "idle_jitter_seconds": 0,
                        "quiet_enabled": True,
                        "quiet_start": "23:00",
                        "quiet_end": "07:00",
                        "schedule_enabled": False,
                        "schedule_times": "",
                        "schedule_jitter_seconds": 0,
                        "sentences": "",
                    }
                )
                await self._save_config()
                self._config_snapshot = None
                reply = "已订阅 MeowTalk。请在插件设置中调整该群的推送参数。"
            else:
                existing["enabled"] = True
                await self._save_config()
                self._config_snapshot = None
                reply = "这个群已经订阅 MeowTalk，现已重新启用。"
        yield event.plain_result(reply)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meowtalk.command("unsubscribe")
    async def unsubscribe(self, event: AstrMessageEvent):
        """Remove the current group from MeowTalk subscriptions."""
        if not event.get_group_id():
            yield event.plain_result("MeowTalk 只能在群聊中退订。")
            return

        async with self._lock:
            groups = self.config.setdefault("groups", [])
            before = len(groups) if isinstance(groups, list) else 0
            if isinstance(groups, list):
                self.config["groups"] = [
                    item
                    for item in groups
                    if not isinstance(item, dict)
                    or item.get("umo") != event.unified_msg_origin
                ]
            removed = before != len(self.config.get("groups", []))
            if removed:
                await self._save_config()
                self._config_snapshot = None
        yield event.plain_result(
            "已退订 MeowTalk。" if removed else "这个群尚未订阅 MeowTalk。"
        )

    @meowtalk.command("status")
    async def status(self, event: AstrMessageEvent):
        """Show the current group's effective MeowTalk configuration."""
        if not event.get_group_id():
            yield event.plain_result("MeowTalk 只能在群聊中查看状态。")
            return
        async with self._lock:
            self._refresh_group_configs(self._now())
            group = self._group_configs.get(event.unified_msg_origin)
        if group is None:
            yield event.plain_result("本群尚未订阅 MeowTalk。")
            return
        schedule = (
            ", ".join(group.schedule_labels)
            if group.schedule_enabled and group.schedule_labels
            else "未启用"
        )
        quiet = (
            f"{group.quiet_start.strftime('%H:%M')}-{group.quiet_end.strftime('%H:%M')}"
            if group.quiet_enabled
            else "未启用"
        )
        yield event.plain_result(
            "MeowTalk 状态\n"
            f"订阅状态: {'启用' if group.enabled else '停用'}\n"
            f"沉寂唤醒: {'开启' if group.idle_enabled else '关闭'}\n"
            f"沉寂时长: {group.idle_timeout_seconds / 60:g} 分钟\n"
            f"免打扰: {quiet}\n"
            f"定时推送: {schedule}\n"
            f"语料数量: {len(group.sentences)}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meowtalk.command("test")
    async def test(self, event: AstrMessageEvent):
        """Send one corpus sentence immediately for testing."""
        if not event.get_group_id():
            yield event.plain_result("MeowTalk 只能在群聊中测试。")
            return
        async with self._lock:
            self._refresh_group_configs(self._now())
            group = self._group_configs.get(event.unified_msg_origin)
            sentence = (
                random.choice(group.sentences)
                if group is not None and group.sentences
                else None
            )
        if group is None:
            yield event.plain_result("本群尚未订阅 MeowTalk。")
            return
        if sentence is None:
            yield event.plain_result("当前没有可用语料，请先在插件设置中填写。")
            return
        try:
            await self.context.send_message(group.umo, MessageChain().message(sentence))
        except Exception as exc:
            logger.exception("MeowTalk test message failed: %s", exc)
            yield event.plain_result(f"发送失败：{exc}")
            return
        yield event.plain_result("测试语料已发送。")

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await self._run_scheduler_tick(self._now())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("MeowTalk scheduler tick failed")
            await asyncio.sleep(1)

    async def _run_scheduler_tick(self, now: datetime) -> None:
        pending_messages: list[tuple[GroupConfig, str]] = []
        async with self._lock:
            self._refresh_group_configs(now)
            active_umos = set(self._group_configs)
            for umo in list(self._group_states):
                if umo not in active_umos:
                    del self._group_states[umo]

            for group in self._group_configs.values():
                if not group.enabled:
                    continue
                state = self._group_states.setdefault(
                    group.umo,
                    GroupRuntime(
                        quiet_active=is_quiet_period(group, now),
                        schedule_started_at=now,
                    ),
                )
                quiet = is_quiet_period(group, now)
                if quiet != state.quiet_active:
                    state.quiet_active = quiet
                    state.idle_due = None
                    state.idle_sent = False
                if (
                    not quiet
                    and group.idle_enabled
                    and state.idle_due is not None
                    and not state.idle_sent
                    and state.idle_due <= now
                ):
                    pending_messages.append((group, "idle"))
                    state.idle_sent = True
                    state.idle_due = None
                pending_messages.extend(
                    self._collect_scheduled_pushes(group, state, now)
                )

            cutoff = now - timedelta(days=3)
            self._scheduled_slots = {
                key: due for key, due in self._scheduled_slots.items() if due >= cutoff
            }

        for group, reason in pending_messages:
            await self._send_random_sentence(group, reason)

    def _collect_scheduled_pushes(
        self,
        group: GroupConfig,
        state: GroupRuntime,
        now: datetime,
    ) -> list[tuple[GroupConfig, str]]:
        if not group.schedule_enabled or not group.schedule_times:
            return []
        pending_messages: list[tuple[GroupConfig, str]] = []
        for day_offset in (-1, 0, 1):
            slot_date = (now + timedelta(days=day_offset)).date()
            for schedule_time in group.schedule_times:
                key = f"{group.umo}|{slot_date.isoformat()}|{schedule_time.strftime('%H:%M')}"
                if key in self._scheduled_slots:
                    continue
                due = scheduled_due(
                    group.umo,
                    slot_date,
                    schedule_time,
                    group.schedule_jitter_seconds,
                    self._timezone,
                )
                if due < state.schedule_started_at:
                    self._scheduled_slots[key] = due
                    continue
                if due <= now:
                    pending_messages.append((group, "scheduled"))
                    self._scheduled_slots[key] = due
        return pending_messages

    async def _send_random_sentence(self, group: GroupConfig, reason: str) -> None:
        if not group.sentences:
            logger.warning("No corpus sentences configured for group %s", group.umo)
            return
        sentence = random.choice(group.sentences)
        try:
            sent = await self.context.send_message(
                group.umo,
                MessageChain().message(sentence),
            )
            if sent is False:
                logger.warning(
                    "MeowTalk %s message was not sent to %s", reason, group.umo
                )
            else:
                logger.info("MeowTalk %s message sent to %s", reason, group.umo)
        except Exception:
            logger.exception("MeowTalk %s message failed for %s", reason, group.umo)

    def _refresh_group_configs(self, now: datetime) -> None:
        snapshot = copy.deepcopy(dict(self.config))
        if snapshot == self._config_snapshot:
            return
        previous = self._group_configs
        groups, errors = build_group_configs(snapshot)
        self._group_configs = groups
        self._config_snapshot = snapshot
        if errors != self._config_errors:
            self._config_errors = errors
            for error in errors:
                logger.warning("MeowTalk config ignored: %s", error)
        for umo, group in groups.items():
            if previous.get(umo) != group:
                self._group_states[umo] = GroupRuntime(
                    quiet_active=is_quiet_period(group, now),
                    schedule_started_at=now,
                )

    async def _save_config(self) -> None:
        save_async = getattr(self.config, "save_config_async", None)
        if callable(save_async):
            await save_async()
            return
        save_sync = getattr(self.config, "save_config", None)
        if callable(save_sync):
            save_sync()

    def _resolve_timezone(self):
        timezone_name = ""
        try:
            timezone_name = str(self.context.get_config().get("timezone", ""))
        except Exception:
            pass
        if timezone_name:
            with contextlib.suppress(Exception):
                return ZoneInfo(timezone_name)
        return datetime.now().astimezone().tzinfo or timezone.utc

    def _now(self) -> datetime:
        return datetime.now(self._timezone)
