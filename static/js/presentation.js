const slides = window.PRESENTATION_SLIDES || [];

const frame = document.getElementById("presentationFrame");

const previousBtn = document.getElementById("previousBtn");
const nextBtn = document.getElementById("nextBtn");
const pauseBtn = document.getElementById("pauseBtn");
const slideIndicator = document.getElementById("slideIndicator");
const controls = document.querySelector(".presentation-overlay");
const loadingBadge = document.getElementById("presentationLoading");

let currentSlide = 0;
let isPlaying = true;

const SLIDE_INTERVAL = 15000;
const DATA_REFRESH_INTERVAL = 600000;

/*
 * The loading pill: shown the instant a fresh page starts loading in the
 * frame (a slide change, a manual Next/Previous, or the periodic database
 * refresh below), hidden once that page has finished loading. A minimum
 * on-screen time keeps it from flashing for a page that returns in a few
 * milliseconds.
 */
const LOADING_MIN_VISIBLE = 300;
let loadingShownAt = 0;

function showLoading() {
    if (!loadingBadge) {
        return;
    }
    loadingShownAt = Date.now();
    loadingBadge.classList.add("is-visible");
}

function hideLoading() {
    if (!loadingBadge) {
        return;
    }
    const elapsed = Date.now() - loadingShownAt;
    const wait = Math.max(0, LOADING_MIN_VISIBLE - elapsed);
    setTimeout(() => {
        loadingBadge.classList.remove("is-visible");
    }, wait);
}


function showControls() {
    if (!controls) {
        return;
    }
    controls.classList.add("is-visible");
    clearTimeout(window.presentationControlsTimer);
    window.presentationControlsTimer = setTimeout(() => {
        controls.classList.remove("is-visible");
    }, 2200);
}


/*
 * Load a presentation slide.
 */
function showSlide(index) {

    if (!slides.length) {
        return;
    }

    currentSlide = index;

    if (currentSlide < 0) {
        currentSlide = slides.length - 1;
    }

    if (currentSlide >= slides.length) {
        currentSlide = 0;
    }

    showLoading();
    frame.src = slides[currentSlide].url;

    slideIndicator.textContent =
        `${currentSlide + 1} / ${slides.length}`;
}

frame.addEventListener("load", () => {
    hideLoading();
    try {
        frame.contentWindow.scrollTo(0, 0);
        if (frame.contentDocument && frame.contentDocument.documentElement) {
            frame.contentDocument.documentElement.scrollTop = 0;
        }
    } catch (error) {
        // Ignore cross-origin or missing-document edge cases while switching slides.
    }
});


/*
 * Move forward.
 */
function nextSlide() {

    showSlide(currentSlide + 1);
}


/*
 * Move backward.
 */
function previousSlide() {

    showSlide(currentSlide - 1);
}


/*
 * Pause/resume automatic presentation.
 */
function togglePlay() {

    isPlaying = !isPlaying;

    pauseBtn.textContent = isPlaying
        ? "❚❚"
        : "▶";

    pauseBtn.title = isPlaying
        ? "Pause"
        : "Resume";
    pauseBtn.setAttribute("aria-label", isPlaying ? "Pause presentation" : "Resume presentation");
}


/*
 * Buttons.
 */
nextBtn.addEventListener("click", nextSlide);

previousBtn.addEventListener("click", previousSlide);

pauseBtn.addEventListener("click", togglePlay);

nextBtn.addEventListener("pointerdown", showControls);
previousBtn.addEventListener("pointerdown", showControls);
pauseBtn.addEventListener("pointerdown", showControls);

document.addEventListener("pointermove", showControls);
document.addEventListener("touchstart", showControls);


/*
 * Keyboard controls.
 */
document.addEventListener("keydown", (event) => {

    if (event.key === "ArrowRight") {
        nextSlide();
    }

    if (event.key === "ArrowLeft") {
        previousSlide();
    }

    if (event.code === "Space") {
        event.preventDefault();
        togglePlay();
    }

});


/*
 * Automatic slide rotation.
 */
setInterval(() => {

    if (isPlaying) {
        nextSlide();
    }

}, SLIDE_INTERVAL);


/*
 * Refresh the currently displayed page periodically.
 *
 * This is useful if the presentation is paused on Analytics Panel
 * or on a loan request for a longer period.
 */
setInterval(() => {

    if (isPlaying === false) {
        return;
    }

    /*
     * Reload the current Django page from the database. This also fires the
     * frame's "load" event once it finishes, so showLoading()/hideLoading()
     * above cover this refresh the same way they cover a normal slide change.
     */
    showLoading();
    frame.contentWindow.location.reload();

}, DATA_REFRESH_INTERVAL);


/*
 * Start on the first slide.
 */
showSlide(0);
showControls();