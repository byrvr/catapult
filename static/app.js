const state = {
    device: null,
    ipaPath: null,
    authed: false,
};

const $ = (sel) => document.querySelector(sel);

const DEVICE_ICONS = {
    ios: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="2" width="14" height="20" rx="2"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg>`,
    ipados: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="3" width="16" height="18" rx="2"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`,
    tvos: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8"/><path d="M12 17v4"/></svg>`,
    unknown: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8"/><path d="M12 17v4"/></svg>`,
};

// ── Device discovery ──

async function refreshDevices() {
    const btn = $("#refreshDevices");
    btn.classList.add("spinning");
    $("#deviceList").innerHTML = '<div class="placeholder">Scanning network...</div>';

    try {
        const resp = await fetch("/api/devices");
        const data = await resp.json();
        renderDevices(data.devices);
    } catch {
        $("#deviceList").innerHTML = '<div class="placeholder">Scan failed — check pymobiledevice3</div>';
    } finally {
        btn.classList.remove("spinning");
    }
}

function renderDevices(devices) {
    const list = $("#deviceList");
    if (!devices.length) {
        list.innerHTML = '<div class="placeholder">No devices found on the network</div>';
        return;
    }

    // Filter out local Mac and non-Apple devices (LG TVs etc)
    const filtered = devices.filter((d) => {
        if (d.host === "127.0.0.1" || d.host.startsWith("fe80::1")) return false;
        if (d.name && d.name.includes("MacBook")) return false;
        const cls = d.device_class || "";
        // Keep Apple devices (ios, tvos, ipados) and unknown that might be Apple
        if (cls === "unknown" && !d.name.includes("Apple") && !d.service.includes("apple")) return false;
        return true;
    });

    if (!filtered.length) {
        list.innerHTML = '<div class="placeholder">No iOS / tvOS devices found</div>';
        return;
    }

    list.innerHTML = filtered
        .map((d) => {
            const cls = d.device_class || "unknown";
            const icon = DEVICE_ICONS[cls] || DEVICE_ICONS.unknown;
            const label = cls === "unknown" ? "device" : cls;
            const needsSetup = !d.installable;
            return `
                <div class="device-item${needsSetup ? ' needs-setup' : ''}" data-udid="${esc(d.udid)}" data-installable="${d.installable}">
                    <span class="device-icon">${icon}</span>
                    <div class="device-meta">
                        <span class="device-name">${esc(d.name)}</span>
                        <span class="device-host">${esc(d.host)}</span>
                    </div>
                    ${needsSetup
                        ? '<button class="btn-setup" title="Pair & setup tunnel">Setup</button>'
                        : `<span class="device-badge">${label}</span>`}
                </div>`;
        })
        .join("");

    list.querySelectorAll(".device-item").forEach((el) => {
        const setupBtn = el.querySelector(".btn-setup");
        if (setupBtn) {
            setupBtn.addEventListener("click", async (e) => {
                e.stopPropagation();
                setupBtn.textContent = "Pairing...";
                setupBtn.disabled = true;
                try {
                    const resp = await fetch("/api/devices/setup", { method: "POST" });
                    const data = await resp.json();
                    if (data.status === "ok") {
                        setupBtn.textContent = "Done!";
                        setTimeout(refreshDevices, 1000);
                    } else {
                        setupBtn.textContent = "Failed";
                        alert(data.message || "Setup failed");
                        setTimeout(() => { setupBtn.textContent = "Setup"; setupBtn.disabled = false; }, 2000);
                    }
                } catch {
                    setupBtn.textContent = "Error";
                    setTimeout(() => { setupBtn.textContent = "Setup"; setupBtn.disabled = false; }, 2000);
                }
            });
        }
        el.addEventListener("click", (e) => {
            if (e.target.closest(".btn-setup")) return; // Handled by setup button
            list.querySelectorAll(".device-item").forEach((e) => e.classList.remove("selected"));
            el.classList.add("selected");
            state.device = el.dataset.udid;
            state.deviceInstallable = el.dataset.installable === "true";
            checkReady();
        });
    });
}

function esc(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
}

// ── IPA upload ──

const dropZone = $("#dropZone");
const ipaFile = $("#ipaFile");

dropZone.addEventListener("click", () => ipaFile.click());
dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropZone.classList.add("drag-over");
});
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("drag-over");
    if (e.dataTransfer.files.length) uploadIpa(e.dataTransfer.files[0]);
});
ipaFile.addEventListener("change", () => {
    if (ipaFile.files.length) uploadIpa(ipaFile.files[0]);
});

async function uploadIpa(file) {
    if (!file.name.endsWith(".ipa")) return;

    dropZone.innerHTML = '<div class="placeholder">Uploading...</div>';
    const form = new FormData();
    form.append("file", file);

    try {
        const resp = await fetch("/api/upload", { method: "POST", body: form });
        if (!resp.ok) {
            const err = await resp.json();
            throw new Error(err.error || "Upload failed");
        }
        const data = await resp.json();
        state.ipaPath = data.path;

        $("#ipaName").textContent = data.info.bundle_name || file.name.replace(".ipa", "");
        $("#ipaBundle").textContent = data.info.bundle_id;
        $("#ipaVersion").textContent =
            data.info.version ? `v${data.info.version} (${data.info.build})` : "";
        $("#ipaMinOS").textContent = data.info.min_os ? `iOS ${data.info.min_os}+` : "";

        dropZone.hidden = true;
        $("#ipaInfo").hidden = false;
        checkReady();
    } catch (e) {
        dropZone.innerHTML = `<div class="placeholder err">${esc(e.message)}</div>`;
        setTimeout(resetDropZone, 2000);
    }
}

function resetDropZone() {
    dropZone.hidden = false;
    dropZone.innerHTML = `
        <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="17 8 12 3 7 8"/>
            <line x1="12" y1="3" x2="12" y2="15"/>
        </svg>
        <p>Drop an IPA file here or <label class="file-link">browse<input type="file" id="ipaFile" accept=".ipa" hidden></label></p>`;
    // Re-bind file input
    const newInput = dropZone.querySelector("#ipaFile");
    if (newInput) newInput.addEventListener("change", () => { if (newInput.files.length) uploadIpa(newInput.files[0]); });
}

$("#clearIpa").addEventListener("click", () => {
    state.ipaPath = null;
    $("#ipaInfo").hidden = true;
    resetDropZone();
    checkReady();
});

// ── Apple ID auth ──

$("#authForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("#authBtn");
    btn.disabled = true;
    btn.textContent = "Signing in...";
    setAuthStatus("");

    try {
        const resp = await fetch("/api/auth/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                apple_id: $("#appleId").value,
                password: $("#applePassword").value,
            }),
        });
        const data = await resp.json();

        if (data.status === "ok") {
            onAuthSuccess();
        } else if (data.status === "2fa_required") {
            $("#authForm").hidden = true;
            $("#tfaSection").hidden = false;
            $("#tfaCode").focus();
            setAuthStatus("Enter the code sent to your trusted devices", "");
        } else {
            setAuthStatus(data.message || "Authentication failed", "err");
        }
    } catch {
        setAuthStatus("Connection error", "err");
    } finally {
        btn.disabled = false;
        btn.textContent = "Sign In";
    }
});

$("#tfaBtn").addEventListener("click", submitTfa);
$("#tfaCode").addEventListener("keydown", (e) => {
    if (e.key === "Enter") submitTfa();
});

async function submitTfa() {
    const btn = $("#tfaBtn");
    const code = $("#tfaCode").value.trim();
    if (code.length < 6) return;

    btn.disabled = true;
    btn.textContent = "Verifying...";

    try {
        const resp = await fetch("/api/auth/2fa", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ code }),
        });
        const data = await resp.json();

        if (data.status === "ok") {
            $("#tfaSection").hidden = true;
            onAuthSuccess();
        } else {
            setAuthStatus(data.message || "Verification failed", "err");
        }
    } catch {
        setAuthStatus("Connection error", "err");
    } finally {
        btn.disabled = false;
        btn.textContent = "Verify";
    }
}

function onAuthSuccess() {
    state.authed = true;
    const section = $("#authSection");
    section.innerHTML = `
        <h2>Apple ID</h2>
        <div class="auth-success">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--success)" stroke-width="2">
                <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
                <polyline points="22 4 12 14.01 9 11.01"/>
            </svg>
            <span>Signed in</span>
        </div>`;
    checkReady();
}

function setAuthStatus(msg, cls) {
    const el = $("#authStatus");
    el.textContent = msg;
    el.className = "auth-status" + (cls ? ` ${cls}` : "");
}

// ── Install ──

function checkReady() {
    $("#installBtn").disabled = !(state.device && state.ipaPath && state.authed);
}

$("#installBtn").addEventListener("click", () => {
    if (!state.device || !state.ipaPath || !state.authed) return;

    const btn = $("#installBtn");
    btn.disabled = true;
    btn.querySelector("span").textContent = "Installing...";
    const container = $("#progressContainer");
    container.hidden = false;

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/install`);

    ws.onopen = () => {
        ws.send(JSON.stringify({
            device_udid: state.device,
            ipa_path: state.ipaPath,
        }));
    };

    ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        const fill = $("#progressFill");
        const text = $("#progressText");

        fill.style.width = `${msg.progress}%`;
        text.textContent = msg.message;

        if (msg.step === "done") {
            fill.classList.add("done");
            text.classList.add("done");
            btn.querySelector("span").textContent = "Done!";
        } else if (msg.step === "error") {
            fill.classList.add("error");
            text.classList.add("error");
            btn.querySelector("span").textContent = "Failed";
        }
    };

    ws.onclose = () => {
        setTimeout(() => {
            container.hidden = true;
            $("#progressFill").style.width = "0%";
            $("#progressFill").className = "progress-fill";
            $("#progressText").className = "progress-text";
            btn.disabled = false;
            btn.querySelector("span").textContent = "Install";
            checkReady();
        }, 4000);
    };

    ws.onerror = () => {
        $("#progressText").textContent = "WebSocket connection failed";
        $("#progressText").classList.add("error");
    };
});

// ── Init ──

async function checkExistingSession() {
    try {
        const resp = await fetch("/api/auth/status");
        const data = await resp.json();
        if (data.authenticated) {
            onAuthSuccess();
        }
    } catch {}
}

refreshDevices();
checkExistingSession();
$("#refreshDevices").addEventListener("click", refreshDevices);
