from aiogram.fsm.state import State, StatesGroup


class AddUserStates(StatesGroup):
    waiting_for_user_selection = State()
    waiting_for_tg_id = State()
    waiting_for_username = State()
    waiting_for_email = State()
    waiting_for_inbound = State()
    waiting_for_limit_gb = State()
    waiting_for_expiry_days = State()


class EditUserStates(StatesGroup):
    waiting_for_new_limit = State()
    waiting_for_days_to_extend = State()
    waiting_for_new_username = State()
    waiting_for_new_tg_id = State()
    waiting_for_new_key_name = State()


class LinkUserStates(StatesGroup):
    waiting_for_tg_id = State()
    waiting_for_server = State()
    waiting_for_client = State()


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
