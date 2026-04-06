const state = {
    device: null,
    ipaPath: null,
    authed: false,
};

const $ = (sel) => document.querySelector(sel);

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
        $("#deviceList").innerHTML = '<div class="placeholder">Failed to scan. Is pymobiledevice3 installed?</div>';
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
    list.innerHTML = devices.map((d) => `
        <div class="device-item" data-udid="${d.udid}">
            <span class="device-dot"></span>
            <span class="device-name">${d.name}</span>
            <span class="device-type">${d.connection || "network"}</span>
        </div>
    `).join("");

    list.querySelectorAll(".device-item").forEach((el) => {
        el.addEventListener("click", () => {
            list.querySelectorAll(".device-item").forEach((e) => e.classList.remove("selected"));
            el.classList.add("selected");
            state.device = el.dataset.udid;
            checkReady();
        });
    });
}

// ── IPA upload ──

const dropZone = $("#dropZone");
const ipaFile = $("#ipaFile");

dropZone.addEventListener("click", () => ipaFile.click());
dropZone.addEventListener("dragover", (e) => { e.preventDefault(); dropZone.classList.add("drag-over"); });
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("drag-over");
    if (e.dataTransfer.files.length) uploadIpa(e.dataTransfer.files[0]);
});
ipaFile.addEventListener("change", () => { if (ipaFile.files.length) uploadIpa(ipaFile.files[0]); });

async function uploadIpa(file) {
    if (!file.name.endsWith(".ipa")) return;

    const form = new FormData();
    form.append("file", file);

    try {
        const resp = await fetch("/api/upload", { method: "POST", body: form });
        const data = await resp.json();
        state.ipaPath = data.path;

        $("#ipaName").textContent = data.info.bundle_name || file.name;
        $("#ipaBundle").textContent = data.info.bundle_id;
        $("#ipaVersion").textContent = `v${data.info.version} (${data.info.build})`;

        dropZone.hidden = true;
        $("#ipaInfo").hidden = false;
        checkReady();
    } catch {
        alert("Failed to process IPA file");
    }
}

$("#clearIpa").addEventListener("click", () => {
    state.ipaPath = null;
    dropZone.hidden = false;
    $("#ipaInfo").hidden = true;
    ipaFile.value = "";
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
            state.authed = true;
            setAuthStatus("Signed in", "ok");
            $("#authForm").hidden = true;
            checkReady();
        } else if (data.status === "2fa_required") {
            $("#tfaSection").hidden = false;
            setAuthStatus("2FA code required", "");
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

$("#tfaBtn").addEventListener("click", async () => {
    const btn = $("#tfaBtn");
    btn.disabled = true;

    try {
        const resp = await fetch("/api/auth/2fa", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ code: $("#tfaCode").value }),
        });
        const data = await resp.json();

        if (data.status === "ok") {
            state.authed = true;
            setAuthStatus("Signed in", "ok");
            $("#authForm").hidden = true;
            $("#tfaSection").hidden = true;
            checkReady();
        } else {
            setAuthStatus(data.message || "Verification failed", "err");
        }
    } catch {
        setAuthStatus("Connection error", "err");
    } finally {
        btn.disabled = false;
    }
});

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

    $("#installBtn").hidden = true;
    const container = $("#progressContainer");
    container.hidden = false;

    const ws = new WebSocket(`ws://${location.host}/ws/install`);

    ws.onopen = () => {
        ws.send(JSON.stringify({
            device_udid: state.device,
            ipa_path: state.ipaPath,
        }));
    };

    ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        $("#progressFill").style.width = `${msg.progress}%`;
        $("#progressText").textContent = msg.message;

        if (msg.step === "done") {
            $("#progressText").style.color = "var(--success)";
        } else if (msg.step === "error") {
            $("#progressText").style.color = "var(--error)";
        }
    };

    ws.onclose = () => {
        setTimeout(() => {
            $("#installBtn").hidden = false;
            container.hidden = true;
            $("#progressFill").style.width = "0%";
            $("#progressText").style.color = "";
        }, 3000);
    };
});

// ── Init ──

refreshDevices();
$("#refreshDevices").addEventListener("click", refreshDevices);
