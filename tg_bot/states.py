from aiogram.fsm.state import State, StatesGroup


class RestoreStates(StatesGroup):
    waiting_for_type = State()
    waiting_for_server = State()
    waiting_for_file = State()


class BackupStates(StatesGroup):
    waiting_for_server = State()


class UserStates(StatesGroup):
    viewing_keys = State()
    viewing_qr = State()
    selected_key_db_id = State()
