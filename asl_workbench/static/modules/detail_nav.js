/* Detail jump bar — a sticky strip of links to the plan detail's panels.
 *
 * The detail view runs to several screens, and the panel an operator wants
 * (the logs, the journal) is usually near the bottom. The bar stays under the
 * header, links only to panels that are showing, and marks the one in view.
 *
 * Buttons rather than `#` links: the hash is the route, so an anchor would
 * navigate away from the plan instead of scrolling within it.
 */

/** Space a jumped-to panel leaves above itself for the header and the bar. */
const NAV_GAP = 8;

let scrollFrame = 0;

export function wireDetailNav() {
  const nav = document.getElementById("wb-detail-nav");
  if (!nav) return;
  nav.addEventListener("click", (event) => {
    const button =
      event.target && typeof event.target.closest === "function"
        ? event.target.closest("[data-nav-target]")
        : null;
    if (!button) return;
    const target = document.getElementById(button.getAttribute("data-nav-target"));
    if (!target) return;
    window.scrollTo({
      top: Math.max(0, window.scrollY + target.getBoundingClientRect().top - navOffset()),
      behavior: "smooth",
    });
  });
  window.addEventListener("scroll", scheduleActiveSync, { passive: true });
  window.addEventListener("resize", syncDetailNav);
  syncDetailNav();
}

/**
 * Hide links to panels that are not showing and mark the one in view.
 *
 * Called after each detail render, since panels come and go with the plan:
 * the unconfirmed-operations panel is usually hidden, and the launch profile
 * is absent remotely.
 */
export function syncDetailNav() {
  const nav = document.getElementById("wb-detail-nav");
  if (!nav) return;
  const header = document.querySelector(".wb-header");
  // The bar sticks under the header, whose height changes with the viewport.
  document.documentElement.style.setProperty(
    "--wb-header-height",
    String(header ? header.offsetHeight : 0) + "px"
  );
  nav.querySelectorAll("[data-nav-target]").forEach((button) => {
    const target = document.getElementById(button.getAttribute("data-nav-target"));
    button.hidden = !isShowing(target);
  });
  syncActiveLink();
}

function scheduleActiveSync() {
  if (scrollFrame) return;
  scrollFrame = window.requestAnimationFrame(() => {
    scrollFrame = 0;
    syncActiveLink();
  });
}

/** The last showing panel whose top has passed under the bar is the one in view. */
function syncActiveLink() {
  const nav = document.getElementById("wb-detail-nav");
  if (!nav || !isShowing(nav)) return;
  const line = navOffset() + 1;
  let active = null;
  nav.querySelectorAll("[data-nav-target]").forEach((button) => {
    if (button.hidden) return;
    const target = document.getElementById(button.getAttribute("data-nav-target"));
    if (target && target.getBoundingClientRect().top <= line) active = button;
  });
  // At the very bottom, the last panels may never reach the line; the last
  // link is the honest answer there.
  const atBottom =
    window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2;
  if (atBottom) {
    const visible = [...nav.querySelectorAll("[data-nav-target]")].filter(
      (button) => !button.hidden
    );
    if (visible.length) active = visible[visible.length - 1];
  }
  nav.querySelectorAll("[data-nav-target]").forEach((button) => {
    const current = button === active;
    if (current) {
      button.setAttribute("aria-current", "true");
    } else {
      button.removeAttribute("aria-current");
    }
  });
  if (active) {
    // Keep the marked link in view on a phone, where the bar scrolls sideways.
    const strip = nav.getBoundingClientRect();
    const link = active.getBoundingClientRect();
    if (link.left < strip.left || link.right > strip.right) {
      nav.scrollLeft += link.left - strip.left - 16;
    }
  }
}

function navOffset() {
  const nav = document.getElementById("wb-detail-nav");
  const header = document.querySelector(".wb-header");
  return (
    (header ? header.offsetHeight : 0) + (nav ? nav.offsetHeight : 0) + NAV_GAP
  );
}

function isShowing(el) {
  return !!(el && !el.hidden && el.getClientRects().length > 0);
}
