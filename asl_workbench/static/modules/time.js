/* One reading of a published moment, for every view that measures one.
 *
 * The two providers spell the same instant differently: the Mac publishes
 * ISO-8601 strings, and the Pi's TEXT columns can hand back an epoch that was
 * published as a float, as "1789466400.0". A view that only knew `Date.parse`
 * read the second as "never", so the queue sort, the idle cell, the preview
 * and the cleanup age each disagreed with the eight-hour window about the
 * same row. They all read through here now.
 */

/**
 * One published moment as epoch seconds, or null. Mirrors
 * overview._activity_epoch.
 *
 * A number of seconds spelled as a string is read as a number, and that is not
 * defensive parsing: the Pi's `catalog_entry.last_activity_at` column is TEXT,
 * so an epoch that was published as a float comes back out of SQLite as
 * "1789466400.0". Reaching `Date.parse` with it yields NaN, the row reads as
 * having never moved, and the eight-hour window then hands it back to the top
 * of the queue -- on the remote deployment only, for a plan the laptop files
 * under "older". The Python half has always read it this way; this is the
 * other half of that rule.
 */
export function toEpochSeconds(value) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value !== "string" || !value.trim()) return null;
  const text = value.trim();
  const numeric = Number(text);
  if (Number.isFinite(numeric)) return numeric;
  const parsed = Date.parse(asUtcWhenUnzoned(text));
  return Number.isFinite(parsed) ? parsed / 1000 : null;
}

/** An ISO date-time with a clock but no offset, which `Date.parse` reads as local. */
const UNZONED_DATE_TIME = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/;

/**
 * Read an offset-less date-time as UTC, as the Python half does.
 *
 * `Date.parse` treats "2026-09-15T08:00:00" as local time while ASL's parser
 * coerces it to UTC, so without this the same row sat on different sides of
 * the eight-hour window in the browser and on the server by the viewer's
 * offset. Strings with an offset, a `Z`, or no clock at all pass unchanged.
 */
function asUtcWhenUnzoned(text) {
  if (!UNZONED_DATE_TIME.test(text)) return text;
  return text.replace(" ", "T") + "Z";
}

/** The same moment in epoch milliseconds, the unit `Date` works in. */
export function toEpochMs(value) {
  const seconds = toEpochSeconds(value);
  return seconds === null ? null : seconds * 1000;
}
