# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Virtual clock that tracks simulated time independently of wall-clock time."""

import asyncio
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from simulator.models.simulation import ClockState

TickCallback = Callable[[datetime, float], Awaitable[None]]


class VirtualClock:
    """Tracks simulated time with configurable speed multiplier."""

    def __init__(
        self,
        start_time: datetime,
        speed_multiplier: float = 10.0,
        tick_interval_seconds: float = 1.0,
    ) -> None:
        self.current_time = start_time
        self.speed_multiplier = speed_multiplier
        self.tick_interval_seconds = tick_interval_seconds
        # semgrep false positive: `is_paused` is a bool attribute/@property, not a method -- no call intended.
        # nosemgrep: is-function-without-parentheses
        self.is_paused = False
        self._callbacks: list[TickCallback] = []
        self._task: asyncio.Task | None = None

    def on_tick(self, callback: TickCallback) -> None:
        """Register a callback invoked each tick with (sim_time, delta_seconds)."""
        self._callbacks.append(callback)

    async def start(self) -> None:
        """Start the clock loop as an asyncio task."""
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Cancel the clock loop."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        """Main tick loop — sleeps for tick_interval then advances time."""
        while True:
            await asyncio.sleep(self.tick_interval_seconds)
            # nosemgrep: is-function-without-parentheses
            if self.is_paused:
                continue
            delta = self.speed_multiplier * self.tick_interval_seconds
            self.current_time += timedelta(seconds=delta)
            for cb in self._callbacks:
                await cb(self.current_time, delta)

    def pause(self) -> None:
        # nosemgrep: is-function-without-parentheses
        self.is_paused = True

    def resume(self) -> None:
        # nosemgrep: is-function-without-parentheses
        self.is_paused = False

    def set_speed(self, multiplier: float) -> None:
        """Set speed multiplier. Must be between 1 and 60."""
        if not 1 <= multiplier <= 60:
            raise ValueError(f"Speed multiplier must be 1–60, got {multiplier}")
        self.speed_multiplier = multiplier

    def jump_to(self, target: datetime) -> None:
        self.current_time = target

    def get_state(self) -> ClockState:
        return ClockState(
            current_time=self.current_time,
            speed_multiplier=self.speed_multiplier,
            # nosemgrep: is-function-without-parentheses
            is_paused=self.is_paused,
        )
