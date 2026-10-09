/* Motion and smooth scrolling shared by every page.
 *
 * Animation is Motion (static/vendor/motion - the library Framer Motion
 * became, used here through its plain-JavaScript API since the page is not a
 * React app) and scrolling is Lenis (static/vendor/lenis). Both are vendored:
 * the Content-Security-Policy allows scripts from this origin only.
 *
 * Everything here degrades to "no animation": if either library fails to
 * load, or the viewer asks for reduced motion, the helpers become no-ops and
 * the page works exactly the same, just without the movement.
 */
(function () {
  "use strict";

  const reduced = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const M = window.Motion;
  const motion = !!(M && M.animate) && !reduced;
  if (motion) document.documentElement.classList.add("has-motion");

  const EASE = [0.16, 1, 0.3, 1];
  const SPRING = { type: "spring", stiffness: 420, damping: 38 };

  function size(target) {
    if (target == null) return 0;
    if (typeof target === "string") return document.querySelectorAll(target).length;
    return typeof target.length === "number" && !(target instanceof Element) ? target.length : 1;
  }

  /* Motion's animate(), or null when there is nothing to move. */
  function animate(target, keyframes, options) {
    if (!motion || !size(target)) return null;
    try {
      return M.animate(target, keyframes, Object.assign({ duration: 0.5, ease: EASE }, options));
    } catch (err) {
      console.warn("animation skipped", err);
      return null;
    }
  }
  const stagger = (step, options) => (motion ? M.stagger(step, options) : 0);

  /* Fade-and-rise entrance for a set of elements, one after another. With
     `scale`, they also grow into place. */
  function enter(targets, options) {
    options = options || {};
    const frames = { opacity: [0, 1], y: [options.y == null ? 14 : options.y, 0] };
    if (options.scale) frames.scale = [options.scale, 1];
    return animate(targets, frames,
      { duration: options.duration || 0.6, delay: stagger(options.step == null ? 0.045 : options.step, { startDelay: options.delay || 0 }) });
  }

  /* Count a number to its value - up from nothing, or on from `from`, the
     value it showed before. `format` turns the number into text. */
  function count(el, to, format, from) {
    from = from || 0;
    if (!motion || to == null || !isFinite(to) || to === from) { el.textContent = format(to); return null; }
    return M.animate(from, to, {
      duration: 1, ease: EASE,
      onUpdate: (v) => { el.textContent = format(v); },
      onComplete: () => { el.textContent = format(to); },
    });
  }

  /* Move a pill (`ink`) to sit exactly behind `target`; both share an offset
     parent. Works in a row or a column - it follows position and size. */
  function slide(ink, target, instant) {
    if (!ink) return;
    if (!target || !target.offsetWidth) {
      ink.style.opacity = "0";
      ink.__placed = false;
      return;
    }
    const to = { x: target.offsetLeft, y: target.offsetTop, width: target.offsetWidth, height: target.offsetHeight, opacity: 1 };
    if (!motion) {
      ink.style.transform = `translate(${to.x}px, ${to.y}px)`;
      ink.style.width = `${to.width}px`;
      ink.style.height = `${to.height}px`;
      ink.style.opacity = "1";
    } else if (instant || !ink.__placed) {
      M.animate(ink, to, { duration: 0 });
    } else {
      M.animate(ink, to, SPRING);
    }
    ink.__placed = true;
  }

  // --- segmented controls: the pressed option's pill slides between them ----

  function segInk(group) {
    let ink = group.querySelector(":scope > .seg-ink");
    if (!ink) {
      ink = document.createElement("span");
      ink.className = "seg-ink";
      ink.setAttribute("aria-hidden", "true");
      group.prepend(ink);
      group.classList.add("has-ink");
    }
    return ink;
  }
  function syncSeg(group, instant) {
    slide(segInk(group), group.querySelector('button[aria-pressed="true"]'), instant);
  }
  function syncSegs(root, instant) {
    (root || document).querySelectorAll(".seg").forEach((group) => syncSeg(group, instant));
  }
  function watchSegs() {
    if (!document.querySelector(".seg")) return;
    syncSegs(document, true);
    const dirty = new Set();
    let queued = false;
    new MutationObserver((records) => {
      for (const record of records) {
        const group = record.target.closest && record.target.closest(".seg");
        if (group) dirty.add(group);
      }
      if (queued || !dirty.size) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        dirty.forEach((group) => syncSeg(group));
        dirty.clear();
      });
    }).observe(document.body, { subtree: true, attributes: true, attributeFilter: ["aria-pressed"] });
  }

  // --- the light that follows the pointer across a glass panel ---------------

  function spotlight() {
    if (reduced || !window.matchMedia("(hover: hover)").matches) return;
    let latest = null;
    let queued = false;
    document.addEventListener("pointermove", (e) => {
      latest = e;
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        const ev = latest;
        const el = ev.target && ev.target.closest ? ev.target.closest(".card, .tile, .insight, .login-card") : null;
        if (!el) return;
        const box = el.getBoundingClientRect();
        el.style.setProperty("--mx", `${ev.clientX - box.left}px`);
        el.style.setProperty("--my", `${ev.clientY - box.top}px`);
      });
    }, { passive: true });
  }

  // --- smooth scrolling -------------------------------------------------------

  const canSmooth = !!window.Lenis && !reduced;

  /* Smooth scrolling inside one scrollable element. Returns a small handle so
     callers can read and set the position whether or not Lenis is running. */
  function smooth(wrapper, content) {
    if (!wrapper) return null;
    if (wrapper.__scroll) return wrapper.__scroll;
    let lenis = null;
    if (canSmooth) {
      // Tells an outer (page-level) Lenis to leave wheel events in here alone.
      wrapper.setAttribute("data-lenis-prevent", "");
      lenis = new window.Lenis({ wrapper, content: content || wrapper.firstElementChild || wrapper, autoRaf: true, lerp: 0.14, wheelMultiplier: 0.9 });
    }
    wrapper.__scroll = {
      get top() { return wrapper.scrollTop; },
      to(top) {
        // Content was just swapped: measure it now, or Lenis clamps the
        // position to the height it remembers from before.
        if (lenis) { lenis.resize(); lenis.scrollTo(top, { immediate: true, force: true }); }
        else wrapper.scrollTop = top;
      },
      resize() { if (lenis) lenis.resize(); },
    };
    return wrapper.__scroll;
  }

  /* Page-level smooth scrolling, only while the page itself scrolls: the
     desktop deck is one fixed screen, and a page-level Lenis there would
     swallow wheel events meant for the charts' zoom sliders. */
  function pageScroll() {
    if (!canSmooth) return;
    const deck = document.body.classList.contains("deck");
    const narrow = window.matchMedia("(max-width: 760px)");
    let lenis = null;
    const apply = () => {
      const wanted = !deck || narrow.matches;
      if (wanted && !lenis) lenis = new window.Lenis({ autoRaf: true, lerp: 0.12 });
      else if (!wanted && lenis) { lenis.destroy(); lenis = null; }
    };
    apply();
    narrow.addEventListener("change", apply);
  }

  // --- page entrance ----------------------------------------------------------

  function reveal() {
    enter(document.querySelectorAll("[data-reveal]"), { step: 0.08, y: 22, duration: 0.8 });
    animate(document.querySelectorAll("[data-pop]"), { opacity: [0, 1], scale: [0.94, 1], y: [26, 0] },
      { type: "spring", stiffness: 180, damping: 22, delay: 0.15 });
  }

  window.UI = { motion, reduced, EASE, SPRING, animate, stagger, enter, count, slide, smooth, syncSegs };

  watchSegs();
  spotlight();
  pageScroll();
  reveal();
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => syncSegs(document, true));
})();
