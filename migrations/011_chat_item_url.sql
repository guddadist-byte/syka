-- The ad's real URL already arrives with every chat (get_chats parses
-- item.url), but it only ever lived in the in-memory cache, so it vanished
-- on restart until the poller happened to revisit that chat. Persist it so
-- an order can be linked back to its listing at any time.

ALTER TABLE chats ADD COLUMN item_url TEXT;
