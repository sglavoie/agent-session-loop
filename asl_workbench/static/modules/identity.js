/* How this front end names a plan, in whichever deployment it is loaded into.
 *
 * The Mac addresses a plan by its path and the Pi addresses it by a digest,
 * because the Pi has never seen the Mac's filesystem and a path it published
 * would be a path it guessed. One front end therefore cannot hard-code
 * `plan_dir=`: it has to ask what the plan in hand is addressable by.
 *
 * Every read and every mutation in the detail view goes through `planUrl` or
 * `planBody`, so the choice is made once here rather than at each of them.
 */

import { state } from "./state.js";

/** The Mac's address: a canonical plan path. */
export const ADDRESS_PLAN_DIR = "plan_dir";
/** The Pi's address: the catalog digest both ends compute the same way. */
export const ADDRESS_PLAN_KEY = "plan_key";

export const ADDRESS_PARAMS = [ADDRESS_PLAN_DIR, ADDRESS_PLAN_KEY];

/**
 * The address a route carries, or null when it carries none yet.
 *
 * A route that names only a run key is the third case, and it is deliberately
 * not an address: a run key has to be resolved against the catalog before
 * anything can be read with it.
 */
export function addressOfRoute(route) {
  if (!route) return null;
  if (route.planDir) {
    return { param: ADDRESS_PLAN_DIR, value: route.planDir };
  }
  if (route.planKey) {
    return { param: ADDRESS_PLAN_KEY, value: route.planKey };
  }
  return null;
}

/** The address of a queue row, preferring the path when the mode has one. */
export function addressOfPlan(plan) {
  if (!plan) return null;
  if (plan.plan_dir) return { param: ADDRESS_PLAN_DIR, value: plan.plan_dir };
  if (plan.plan_key) return { param: ADDRESS_PLAN_KEY, value: plan.plan_key };
  return null;
}

/**
 * The stable per-row key: a path where there is one, the digest otherwise.
 *
 * Used for the keyed DOM refresh, the selection, and the cleanup set, so a
 * row survives a poll in either mode rather than being rebuilt each time.
 */
export function planIdOf(plan) {
  const address = addressOfPlan(plan);
  return address ? address.value : "";
}

/** The identity of the plan the route is on, or "" when it is on none. */
export function routePlanId() {
  const address = addressOfRoute(state.route);
  return address ? address.value : "";
}

/** True when this row is the run a `#run=` deep link was written about. */
export function planMatchesRunKey(plan, runKey) {
  if (!plan || !runKey) return false;
  return plan.run_key === runKey;
}

/** The current route's address, as a query fragment. */
export function planQuery(extra) {
  const address = addressOfRoute(state.route);
  const parts = [];
  if (address) {
    parts.push(address.param + "=" + encodeURIComponent(address.value));
  }
  if (extra) {
    Object.keys(extra).forEach((name) => {
      const value = extra[name];
      if (value === undefined || value === null || value === "") return;
      parts.push(name + "=" + encodeURIComponent(value));
    });
  }
  return parts.join("&");
}

/** A detail read against the plan the route is on. */
export function planUrl(path, extra) {
  const query = planQuery(extra);
  return query ? path + "?" + query : path;
}

/**
 * The run the detail view is looking at, as both ends name it.
 *
 * Preferred from the loaded plan rather than from the route, because a route
 * that arrived as `#plan?key=` names no run and the plan always does. Both
 * modes publish this field, computed by the catalog's own function from the
 * same inputs, so it means the same run either way.
 */
export function routeRunKey() {
  if (state.detailPlan && state.detailPlan.run_key) {
    return state.detailPlan.run_key;
  }
  return (state.route && state.route.runKey) || null;
}

/**
 * The address fields a mutation body has to carry, plus anything else.
 *
 * The run key goes with them: the remote service's operations journal has no
 * column for a submission without one, so a mutation that omitted it would be
 * admitted and then fail at the insert -- on its way out with no durable
 * record that it left. Sent in both modes because the local mode ignores it,
 * and one body shape is easier to be right about than two.
 */
export function planBody(extra) {
  const address = addressOfRoute(state.route);
  const body = {};
  if (address) body[address.param] = address.value;
  const runKey = routeRunKey();
  if (runKey) body.run_key = runKey;
  return Object.assign(body, extra || {});
}

/* What a plan is *called*, in whichever deployment published it.
 *
 * The two providers name the same three things differently, and neither is
 * wrong: the laptop derives `label`, `searchable_text` and `next_action_hint`
 * from the plan on disk, while the Pi carries `display_label`/`title` and
 * `search_text` from the observation the connector sent and has no hint at all
 * because it has not read the plan. Left unreconciled, every row on the Pi
 * renders as "(unknown plan)" -- the fallback is the path's last segment, and
 * the Pi has no path.
 *
 * So the shell asks here rather than reading a field name directly. This is
 * the same choice `addressOfPlan` makes one screen up: one front end, two
 * vocabularies, reconciled once.
 */

/** The name to show for a plan, falling back to the last path segment. */
export function planLabel(plan) {
  if (!plan) return "(unknown plan)";
  const named = plan.label || plan.display_label || plan.title;
  if (named) return String(named);
  const planDir = plan.plan_dir;
  if (!planDir) return "(unknown plan)";
  const parts = String(planDir).split("/");
  return parts[parts.length - 1] || String(planDir);
}

/** The pre-folded text the queue search matches against. */
export function planSearchText(plan) {
  if (!plan) return "";
  const text = plan.searchable_text || plan.search_text;
  return typeof text === "string" ? text : "";
}

/**
 * The sentence under a row: what to do next, or why the plan is where it is.
 *
 * The Pi publishes neither, by design -- a hint is a reading of the plan, and
 * the observation carries the state rather than the reading. `activity_label`
 * is the nearest thing it has, and it is the last resort rather than the first
 * because the laptop shows it in its own place.
 */
export function planHint(plan) {
  if (!plan) return "";
  const hint =
    plan.next_action_hint || plan.detail || plan.concise_reason ||
    plan.activity_label;
  return typeof hint === "string" ? hint : "";
}

/** The deep link that opens one plan, in the form this mode can resolve. */
export function detailHash(address, focus) {
  if (!address) return "";
  const name = address.param === ADDRESS_PLAN_KEY ? "key" : "dir";
  let hash = "#plan?" + name + "=" + encodeURIComponent(address.value);
  if (focus) hash += "&focus=" + encodeURIComponent(focus);
  return hash;
}

/** The deep link for a queue row. */
export function detailHashForPlan(plan, focus) {
  return detailHash(addressOfPlan(plan), focus);
}
