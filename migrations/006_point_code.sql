-- Short, stable internal code per point (e.g. "ТКЧ"), separate from the
-- human-editable display name (points.name can be freely renamed by an
-- admin without breaking anything that references the point by code).
-- Used by: bulk address/hours import (matching by code) and template
-- placeholders (!<CODE>А / !<CODE>В substituted with address/hours).

ALTER TABLE points ADD COLUMN code TEXT;
