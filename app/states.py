"""FSM states for admin flows."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AdminCategorySG(StatesGroup):
    title = State()


class AdminProductSG(StatesGroup):
    category = State()
    title = State()
    description = State()
    price = State()
    delivery_type = State()
    payload = State()
    keys = State()


class AdminKeysSG(StatesGroup):
    waiting = State()


class AdminBroadcastSG(StatesGroup):
    waiting = State()
    confirm = State()
