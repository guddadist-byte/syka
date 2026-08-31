-- Client photos were detected but thrown away: get_messages() only kept a
-- has_image boolean, so a photo rendered as the placeholder "(фото)" /
-- "📷 Фото" and the actual picture was unreachable. Keep the URL so the
-- dialog can show the photo itself, and so it survives a restart instead
-- of living only in the in-memory cache.

ALTER TABLE messages ADD COLUMN image_url TEXT;
