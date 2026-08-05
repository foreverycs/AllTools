/**
 * Toolkit — Sound Effects System
 * Synthesizes all UI sounds via Web Audio API. Zero external files.
 * Exposes window.TKSound global API and auto-attaches via event delegation.
 */
(function () {
    'use strict';

    // ======================== CONFIG ========================
    const DEFAULT_VOLUME = 0.35;

    let ctx = null;       // AudioContext (lazy-init)
    let enabled = true;
    let masterGain = null;
    let initialized = false;

    function ensureCtx() {
        if (ctx) return true;
        try {
            ctx = new (window.AudioContext || window.webkitAudioContext)();
            masterGain = ctx.createGain();
            masterGain.gain.value = DEFAULT_VOLUME;
            masterGain.connect(ctx.destination);
            initialized = true;
            return true;
        } catch (e) {
            return false;
        }
    }

    // Resume context on first user gesture (browser autoplay policy)
    function resumeOnGesture() {
        if (ctx && ctx.state === 'suspended') ctx.resume();
    }

    // ======================== SOUND DEFINITIONS ========================
    const sounds = {

        // Subtle soft tick — hover on interactive elements
        hover: function () {
            if (!ensureCtx()) return;
            const t = ctx.currentTime;
            const osc = ctx.createOscillator();
            const g = ctx.createGain();
            osc.type = 'sine';
            osc.frequency.setValueAtTime(3200, t);
            osc.frequency.exponentialRampToValueAtTime(2800, t + 0.04);
            g.gain.setValueAtTime(0.08, t);
            g.gain.exponentialRampToValueAtTime(0.001, t + 0.05);
            osc.connect(g).connect(masterGain);
            osc.start(t);
            osc.stop(t + 0.05);
        },

        // Satisfying click/tap — button presses
        click: function () {
            if (!ensureCtx()) return;
            const t = ctx.currentTime;
            const osc = ctx.createOscillator();
            const g = ctx.createGain();
            osc.type = 'sine';
            osc.frequency.setValueAtTime(1800, t);
            osc.frequency.exponentialRampToValueAtTime(1200, t + 0.06);
            g.gain.setValueAtTime(0.15, t);
            g.gain.exponentialRampToValueAtTime(0.001, t + 0.08);
            osc.connect(g).connect(masterGain);
            osc.start(t);
            osc.stop(t + 0.08);

            const osc2 = ctx.createOscillator();
            const g2 = ctx.createGain();
            osc2.type = 'triangle';
            osc2.frequency.setValueAtTime(800, t);
            osc2.frequency.exponentialRampToValueAtTime(600, t + 0.05);
            g2.gain.setValueAtTime(0.08, t);
            g2.gain.exponentialRampToValueAtTime(0.001, t + 0.06);
            osc2.connect(g2).connect(masterGain);
            osc2.start(t);
            osc2.stop(t + 0.06);
        },

        // Pleasant ascending chime — success / done
        success: function () {
            if (!ensureCtx()) return;
            const t = ctx.currentTime;
            const notes = [523.25, 659.25, 783.99]; // C5, E5, G5
            notes.forEach((freq, i) => {
                const osc = ctx.createOscillator();
                const g = ctx.createGain();
                osc.type = 'sine';
                osc.frequency.setValueAtTime(freq, t + i * 0.09);
                g.gain.setValueAtTime(0, t);
                g.gain.linearRampToValueAtTime(0.18, t + i * 0.09);
                g.gain.exponentialRampToValueAtTime(0.001, t + i * 0.09 + 0.25);
                osc.connect(g).connect(masterGain);
                osc.start(t + i * 0.09);
                osc.stop(t + i * 0.09 + 0.25);
            });
        },

        // Subtle descending tone — error / failure
        error: function () {
            if (!ensureCtx()) return;
            const t = ctx.currentTime;
            const osc = ctx.createOscillator();
            const g = ctx.createGain();
            osc.type = 'sawtooth';
            osc.frequency.setValueAtTime(400, t);
            osc.frequency.exponentialRampToValueAtTime(200, t + 0.2);
            g.gain.setValueAtTime(0.1, t);
            g.gain.exponentialRampToValueAtTime(0.001, t + 0.25);
            osc.connect(g).connect(masterGain);
            osc.start(t);
            osc.stop(t + 0.25);

            const osc2 = ctx.createOscillator();
            const g2 = ctx.createGain();
            osc2.type = 'sine';
            osc2.frequency.setValueAtTime(300, t + 0.05);
            osc2.frequency.exponentialRampToValueAtTime(150, t + 0.22);
            g2.gain.setValueAtTime(0.08, t + 0.05);
            g2.gain.exponentialRampToValueAtTime(0.001, t + 0.25);
            osc2.connect(g2).connect(masterGain);
            osc2.start(t + 0.05);
            osc2.stop(t + 0.25);
        },

        // Soft whoosh/rise — modal open
        modalOpen: function () {
            if (!ensureCtx()) return;
            const t = ctx.currentTime;
            const osc = ctx.createOscillator();
            const g = ctx.createGain();
            osc.type = 'sine';
            osc.frequency.setValueAtTime(200, t);
            osc.frequency.exponentialRampToValueAtTime(600, t + 0.15);
            g.gain.setValueAtTime(0.1, t);
            g.gain.exponentialRampToValueAtTime(0.001, t + 0.2);
            osc.connect(g).connect(masterGain);
            osc.start(t);
            osc.stop(t + 0.2);

            const osc2 = ctx.createOscillator();
            const g2 = ctx.createGain();
            osc2.type = 'triangle';
            osc2.frequency.setValueAtTime(400, t);
            osc2.frequency.exponentialRampToValueAtTime(900, t + 0.12);
            g2.gain.setValueAtTime(0.05, t);
            g2.gain.exponentialRampToValueAtTime(0.001, t + 0.18);
            osc2.connect(g2).connect(masterGain);
            osc2.start(t);
            osc2.stop(t + 0.18);
        },

        // Soft descending — modal close
        modalClose: function () {
            if (!ensureCtx()) return;
            const t = ctx.currentTime;
            const osc = ctx.createOscillator();
            const g = ctx.createGain();
            osc.type = 'sine';
            osc.frequency.setValueAtTime(500, t);
            osc.frequency.exponentialRampToValueAtTime(200, t + 0.12);
            g.gain.setValueAtTime(0.08, t);
            g.gain.exponentialRampToValueAtTime(0.001, t + 0.15);
            osc.connect(g).connect(masterGain);
            osc.start(t);
            osc.stop(t + 0.15);
        },

        // Quick pop — toggle switch
        toggle: function () {
            if (!ensureCtx()) return;
            const t = ctx.currentTime;
            const osc = ctx.createOscillator();
            const g = ctx.createGain();
            osc.type = 'sine';
            osc.frequency.setValueAtTime(1000, t);
            osc.frequency.exponentialRampToValueAtTime(1400, t + 0.04);
            g.gain.setValueAtTime(0.12, t);
            g.gain.exponentialRampToValueAtTime(0.001, t + 0.06);
            osc.connect(g).connect(masterGain);
            osc.start(t);
            osc.stop(t + 0.06);
        },

        // Notification bubble — toast appearing
        notify: function () {
            if (!ensureCtx()) return;
            const t = ctx.currentTime;
            const osc = ctx.createOscillator();
            const g = ctx.createGain();
            osc.type = 'sine';
            osc.frequency.setValueAtTime(880, t);
            osc.frequency.exponentialRampToValueAtTime(1100, t + 0.06);
            osc.frequency.exponentialRampToValueAtTime(880, t + 0.12);
            g.gain.setValueAtTime(0.12, t);
            g.gain.exponentialRampToValueAtTime(0.001, t + 0.18);
            osc.connect(g).connect(masterGain);
            osc.start(t);
            osc.stop(t + 0.18);
        },

        // Copy to clipboard — short blip
        copy: function () {
            if (!ensureCtx()) return;
            const t = ctx.currentTime;
            const osc = ctx.createOscillator();
            const g = ctx.createGain();
            osc.type = 'square';
            osc.frequency.setValueAtTime(1200, t);
            osc.frequency.exponentialRampToValueAtTime(1600, t + 0.03);
            g.gain.setValueAtTime(0.06, t);
            g.gain.exponentialRampToValueAtTime(0.001, t + 0.05);
            osc.connect(g).connect(masterGain);
            osc.start(t);
            osc.stop(t + 0.05);

            const osc2 = ctx.createOscillator();
            const g2 = ctx.createGain();
            osc2.type = 'square';
            osc2.frequency.setValueAtTime(1600, t + 0.05);
            osc2.frequency.exponentialRampToValueAtTime(2000, t + 0.08);
            g2.gain.setValueAtTime(0.06, t + 0.05);
            g2.gain.exponentialRampToValueAtTime(0.001, t + 0.1);
            osc2.connect(g2).connect(masterGain);
            osc2.start(t + 0.05);
            osc2.stop(t + 0.1);
        }
    };

    // ======================== PLAY ========================
    function play(name) {
        if (!enabled) return;
        if (!ctx || ctx.state !== 'running') return;
        if (sounds[name]) sounds[name]();
    }

    // ======================== AUTO-ATTACH VIA EVENT DELEGATION ========================
    const HOVER_SELECTORS = [
        'button',
        'a[href]',
        '.tool-card',
        '.tc',
        '.tkm-fr-item',
        '.tkm-email-card',
        '.tk-footer-icon-btn',
        '[data-sound-hover]'
    ].join(',');

    const CLICK_SELECTORS = [
        'button',
        'input[type="submit"]',
        '.tc',
        '.tk-footer-icon-btn',
        '[data-sound-click]'
    ].join(',');

    let lastHoverTime = 0;

    function attachDelegation() {
        document.addEventListener('mouseover', function (e) {
            const target = e.target.closest(HOVER_SELECTORS);
            if (!target) return;
            const now = Date.now();
            if (now - lastHoverTime < 80) return;
            lastHoverTime = now;
            play('hover');
        }, { passive: true });

        document.addEventListener('click', function (e) {
            resumeOnGesture();
            const target = e.target.closest(CLICK_SELECTORS);
            if (!target) return;
            play('click');
        }, { passive: true });
    }

    // ======================== INIT ========================
    function init() {
        attachDelegation();
        ['click', 'touchstart', 'keydown'].forEach(evt => {
            document.addEventListener(evt, function initAudio() {
                ensureCtx();
                resumeOnGesture();
                document.removeEventListener(evt, initAudio);
            }, { once: true });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // ======================== GLOBAL API ========================
    window.TKSound = {
        play: play,
        isEnabled: function () { return enabled; }
    };

})();
