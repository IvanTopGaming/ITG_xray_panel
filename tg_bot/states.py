from aiogram.fsm.state import State, StatesGroup


class UserStates(StatesGroup):
    viewing_keys = State()
