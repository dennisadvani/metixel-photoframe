// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

/**
 * Metixel Photoframe Dashboard — login gate module.
 *
 * Shows a full-screen login overlay when the optional web password is set
 * and the session is not authenticated.  Handles the login form (single
 * password field, Enter-to-submit, inline error) and exposes showLogin /
 * hideLogin for the SPA boot gate and the 401/403 handler in core.js.
 */

import { apiPost, showToast } from "./core.js";

var _overlay = null;
var _form = null;
var _passwordInput = null;
var _errorEl = null;
var _onLoginSuccess = null;

function _init() {
    if (_overlay) return;
    _overlay = document.getElementById("login-overlay");
    _form = document.getElementById("login-form");
    _passwordInput = document.getElementById("login-password");
    _errorEl = document.getElementById("login-error");
    if (_form) {
        _form.addEventListener("submit", function (e) {
            e.preventDefault();
            _submit();
        });
    }
}

function _setError(msg) {
    if (!_errorEl) return;
    if (msg) {
        _errorEl.textContent = msg;
        _errorEl.style.display = "block";
    } else {
        _errorEl.style.display = "none";
    }
}

async function _submit() {
    if (!_passwordInput) return;
    var password = _passwordInput.value;
    if (!password) {
        _setError("Please enter a password.");
        return;
    }
    var btn = document.getElementById("btn-login");
    var original = btn ? btn.innerHTML : "";
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = "Unlocking…";
    }
    _setError("");
    try {
        var result = await apiPost("/auth/login", { password: password });
        if (result && result.authenticated) {
            _passwordInput.value = "";
            hideLogin();
            if (_onLoginSuccess) _onLoginSuccess();
            showToast("Unlocked", "success");
        } else {
            var msg = (result && result.message) || "Incorrect password.";
            _setError(msg);
        }
    } catch (err) {
        _setError("Could not reach the backend. Try again.");
    } finally {
        if (btn) {
            btn.innerHTML = original;
            btn.disabled = false;
        }
        if (_passwordInput) _passwordInput.focus();
    }
}

/**
 * Show the login overlay.
 * @param {Function} [onSuccess] - Called after a successful login.
 */
export function showLogin(onSuccess) {
    _init();
    _onLoginSuccess = onSuccess || null;
    if (_overlay) _overlay.style.display = "flex";
    if (_passwordInput) {
        _passwordInput.value = "";
        setTimeout(function () { _passwordInput.focus(); }, 50);
    }
    _setError("");
}

/**
 * Hide the login overlay.
 */
export function hideLogin() {
    _init();
    if (_overlay) _overlay.style.display = "none";
}

/**
 * Return whether the login overlay is currently visible.
 * @returns {boolean}
 */
export function isLoginVisible() {
    _init();
    return !!_overlay && _overlay.style.display === "flex";
}