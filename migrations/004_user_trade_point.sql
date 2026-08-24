-- Free-text "which trade point does this person work at" label, collected
-- at registration (right after ФИО) and editable later from "Все
-- пользователи" — informational only, separate from the formal
-- point-subscription/routing system (subscriptions, responsible_point_id).

ALTER TABLE users ADD COLUMN trade_point_name TEXT;
