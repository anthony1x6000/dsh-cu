/**
 * Computer use on KDE Plasma for DeepSeek Harness.
 *
 * Registers the model-facing tool surface — windows, screenshots, pointer and
 * keyboard input, the accessibility tree, app launch, flatpak, clipboard — and
 * delegates every desktop operation to a bundled Python bridge
 * (`bin/dsh-cu-bridge`) that it spawns once and keeps resident.
 *
 * Why a bridge instead of doing the work here:
 *
 * * Input injection is EIS over a socket that KWin hands out with a cookie;
 *   libei (a C library) speaks that protocol, and the handshake must be kept
 *   alive across calls.
 * * PyGObject's AT-SPI binding binds its D-Bus connections once per process, so
 *   the accessibility tree wants a long-lived process too.
 *
 * The two halves of the flatpak story live on both sides of this boundary and
 * are the reason the plugin exists: KWin reports a sandboxed app's own pid while
 * AT-SPI reports the sandbox's `xdg-dbus-proxy` pid, and only the instance's
 * `app-flatpak-<app-id>-<n>.scope` cgroup ties them together. Window listing and
 * the accessibility tree therefore report a `flatpakAppId`, and app launch waits
 * for the window whose scope belongs to the app it started instead of guessing.
 *
 * Definitions are built by hand against the registry's raw JSON Schema contract
 * (the same shape `@deepseek-ai/dsh-mcp-client` builds MCP tools from), so this
 * file imports no package — only Node builtins — and can be loaded from a
 * profile patch with no install step, exactly like the reference plugin.
 *
 * Derived from kwin-mcp (MIT, Copyright (c) 2026 Byeonghoon Yoo) — see NOTICE.
 *
 * @module dsh-cu
 */
import { spawn } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
/** Cordis plugin name used by loader diagnostics. */
export const name = 'computer-use-plasma';
/** The tool registry is the one hard dependency; the bridge is loaded from config. */
export const inject = ['tools'];
/** Default interpreter used to launch the bridge. */
export const DEFAULT_PYTHON = 'python3';
/** Relative path of the bridge launcher inside this package. */
export const LAUNCHER_RELATIVE_PATH = 'bin/dsh-cu-bridge';
/** Default prefix every registered tool name starts with. */
export const DEFAULT_TOOL_PREFIX = 'cu_';
/** Default per-request budget for a bridge operation (ms). */
export const DEFAULT_REQUEST_TIMEOUT_MS = 60_000;
/** Default budget for `cu_launch`, which waits for a window to appear (ms). */
export const DEFAULT_LAUNCH_TIMEOUT_MS = 20_000;
/** Default long-edge bound for screenshots; 0 keeps the capture at full size. */
export const DEFAULT_SCREENSHOT_MAX_EDGE = 0;
/** How long a disposed bridge gets to exit on its own before it is killed (ms). */
export const DISPOSE_GRACE_MS = 2_000;
/** A transport- or operation-level failure, carrying the bridge's own code. */
export class BridgeError extends Error {
    /** Machine-routable code from the bridge (`NO_SUCH_WINDOW`, `EIS_UNAVAILABLE`, ...). */
    code;
    /** Optional structured detail from the bridge. */
    detail;
    /**
     * @param message - human-readable failure.
     * @param code - machine-routable code.
     * @param detail - optional structured detail.
     */
    constructor(message, code = 'BRIDGE_ERROR', detail) {
        super(message);
        this.name = 'BridgeError';
        this.code = code;
        this.detail = detail;
    }
}
/** Read a positive integer out of plugin config, or fall back. */
function positiveInt(value, fallback, label) {
    if (value === undefined)
        return fallback;
    if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) {
        throw new Error(`dsh-cu: ${label} must be a positive number, got ${String(value)}`);
    }
    return value;
}
/** Read an optional string out of plugin config. */
function optionalString(value, label) {
    if (value === undefined)
        return undefined;
    if (typeof value !== 'string')
        throw new Error(`dsh-cu: ${label} must be a string`);
    const trimmed = value.trim();
    return trimmed.length > 0 ? trimmed : undefined;
}
/**
 * Apply defaults and validate one plugin config object.
 * @param config - raw plugin config, possibly empty.
 * @returns the resolved configuration.
 */
export function resolveConfig(config = {}) {
    const env = {};
    for (const [key, value] of Object.entries(config.env ?? {})) {
        env[key] = String(value);
    }
    const prefix = config.toolPrefix ?? DEFAULT_TOOL_PREFIX;
    if (!/^[A-Za-z][A-Za-z0-9_-]*$/u.test(prefix)) {
        throw new Error(`dsh-cu: toolPrefix must match [A-Za-z][A-Za-z0-9_-]*, got ${JSON.stringify(prefix)}`);
    }
    return {
        python: config.python ?? process.env.DSH_CU_PYTHON ?? DEFAULT_PYTHON,
        bridgePath: optionalString(config.bridgePath, 'bridgePath'),
        scriptDir: optionalString(config.scriptDir, 'scriptDir') ?? optionalString(process.env.DSH_CU_SCRIPT_DIR, 'DSH_CU_SCRIPT_DIR'),
        stateDir: optionalString(config.stateDir, 'stateDir') ?? optionalString(process.env.DSH_CU_STATE_DIR, 'DSH_CU_STATE_DIR'),
        screenshotDir: optionalString(config.screenshotDir, 'screenshotDir')
            ?? optionalString(process.env.DSH_CU_SCREENSHOT_DIR, 'DSH_CU_SCREENSHOT_DIR'),
        requestTimeoutMs: positiveInt(config.requestTimeoutMs, DEFAULT_REQUEST_TIMEOUT_MS, 'requestTimeoutMs'),
        launchTimeoutMs: positiveInt(config.launchTimeoutMs, DEFAULT_LAUNCH_TIMEOUT_MS, 'launchTimeoutMs'),
        toolPrefix: prefix,
        enableInput: config.enableInput !== false,
        enableSkill: config.enableSkill !== false,
        env,
    };
}
/**
 * Walk up from a module URL until the `package.json` naming this package is
 * found, so the plugin works both from `lib/` and straight from `index.ts`.
 * @param startDir - directory of the running module.
 * @returns the package root, or the start directory when no marker is found.
 */
export function findPackageRoot(startDir) {
    let dir = resolve(startDir);
    for (;;) {
        const manifest = join(dir, 'package.json');
        if (existsSync(manifest)) {
            try {
                const parsed = JSON.parse(readFileSync(manifest, 'utf8'));
                if (parsed.name === 'dsh-cu')
                    return dir;
            }
            catch {
                // A malformed ancestor manifest is not this plugin's problem.
            }
        }
        const parent = dirname(dir);
        if (parent === dir)
            return resolve(startDir);
        dir = parent;
    }
}
/** Directory of the running module. */
export function moduleDir() {
    return dirname(fileURLToPath(import.meta.url));
}
/** This package's root directory, resolved from the running module. */
export function packageRoot() {
    return findPackageRoot(moduleDir());
}
/**
 * Resolve the launcher path for this installation.
 * @param config - resolved configuration.
 * @returns the absolute launcher path.
 */
export function launcherPath(config) {
    if (config.bridgePath !== undefined)
        return config.bridgePath;
    return join(packageRoot(), LAUNCHER_RELATIVE_PATH);
}
/** One newline-delimited JSON-RPC client over a resident bridge process. */
export class BridgeClient {
    config;
    launcher;
    child;
    buffer = '';
    nextId = 1;
    pending = new Map();
    disposed = false;
    /**
     * @param config - resolved plugin configuration.
     * @param launcher - absolute path of the bridge launcher.
     */
    constructor(config, launcher) {
        this.config = config;
        this.launcher = launcher;
    }
    /** Whether the bridge process is currently running. */
    get running() {
        return this.child !== undefined && this.child.exitCode === null && !this.child.killed;
    }
    /** Environment for the bridge child: the session plus every configured override. */
    childEnv() {
        const env = { ...process.env, ...this.config.env, DSH_CU_PYTHON: this.config.python };
        if (this.config.scriptDir !== undefined)
            env.DSH_CU_SCRIPT_DIR = this.config.scriptDir;
        if (this.config.stateDir !== undefined)
            env.DSH_CU_STATE_DIR = this.config.stateDir;
        if (this.config.screenshotDir !== undefined)
            env.DSH_CU_SCREENSHOT_DIR = this.config.screenshotDir;
        return env;
    }
    /** Start the bridge process when it is not already running. */
    ensureChild() {
        if (this.disposed)
            throw new BridgeError('the dsh-cu plugin has been disposed', 'DISPOSED');
        if (this.running)
            return this.child;
        if (!existsSync(this.launcher)) {
            throw new BridgeError(`the dsh-cu bridge launcher is missing at ${this.launcher}; reinstall the dsh-cu bundle `
                + 'or point config.bridgePath at it', 'BRIDGE_MISSING');
        }
        const child = spawn(this.launcher, ['--serve'], {
            env: this.childEnv(),
            stdio: ['pipe', 'pipe', 'pipe'],
            windowsHide: true,
        });
        child.stdout?.setEncoding('utf8');
        child.stdout?.on('data', (chunk) => this.onData(chunk));
        child.stderr?.setEncoding('utf8');
        child.stderr?.on('data', (chunk) => {
            for (const line of String(chunk).split('\n')) {
                if (line.trim().length > 0)
                    console.error(`[dsh-cu] ${line.trim()}`);
            }
        });
        child.on('exit', (code, signal) => this.onExit(code, signal));
        child.on('error', (error) => {
            this.failAll(new BridgeError(`the dsh-cu bridge failed to start: ${String(error)}`, 'BRIDGE_SPAWN_FAILED'));
        });
        this.child = child;
        return child;
    }
    /** Handle one chunk of the response stream, framing on newlines. */
    onData(chunk) {
        this.buffer += chunk;
        for (;;) {
            const newline = this.buffer.indexOf('\n');
            if (newline < 0)
                return;
            const line = this.buffer.slice(0, newline).trim();
            this.buffer = this.buffer.slice(newline + 1);
            if (line.length === 0)
                continue;
            this.onFrame(line);
        }
    }
    /** Resolve or reject one response frame. */
    onFrame(line) {
        let frame;
        try {
            frame = JSON.parse(line);
        }
        catch {
            console.error(`[dsh-cu] ignoring an unparsable response line: ${line.slice(0, 200)}`);
            return;
        }
        const id = typeof frame.id === 'number' ? frame.id : undefined;
        if (id === undefined)
            return;
        const pending = this.pending.get(id);
        if (pending === undefined)
            return;
        this.pending.delete(id);
        clearTimeout(pending.timer);
        if (frame.ok === true) {
            pending.resolve(frame.result);
            return;
        }
        const code = typeof frame.error?.code === 'string' ? frame.error.code : 'BRIDGE_ERROR';
        const message = typeof frame.error?.message === 'string' ? frame.error.message : 'the bridge reported a failure';
        pending.reject(new BridgeError(message, code, frame.error?.detail));
    }
    /** Reject every in-flight request when the child disappears. */
    onExit(code, signal) {
        this.child = undefined;
        const reason = `the dsh-cu bridge exited (code ${String(code)}, signal ${String(signal)})`;
        this.failAll(new BridgeError(reason, 'BRIDGE_EXITED'));
    }
    /** Reject every pending request with one error. */
    failAll(error) {
        for (const [, pending] of this.pending) {
            clearTimeout(pending.timer);
            pending.reject(error);
        }
        this.pending.clear();
    }
    /**
     * Run one operation on the bridge.
     * @param op - bridge operation name (`window.list`, `mouse`, ...).
     * @param args - operation arguments.
     * @param options - per-call signal and timeout budget.
     * @returns the operation result as parsed JSON.
     */
    async request(op, args, options = {}) {
        if (options.signal?.aborted === true) {
            throw new BridgeError('the call was cancelled before it started', 'CANCELLED');
        }
        const child = this.ensureChild();
        const id = this.nextId++;
        const timeoutMs = options.timeoutMs ?? this.config.requestTimeoutMs;
        return await new Promise((resolvePromise, rejectPromise) => {
            const timer = setTimeout(() => {
                this.pending.delete(id);
                rejectPromise(new BridgeError(`the dsh-cu bridge did not answer ${op} within ${timeoutMs}ms`, 'BRIDGE_TIMEOUT'));
            }, timeoutMs);
            timer.unref?.();
            const onAbort = () => {
                const pending = this.pending.get(id);
                if (pending === undefined)
                    return;
                this.pending.delete(id);
                clearTimeout(pending.timer);
                pending.reject(new BridgeError('the call was cancelled', 'CANCELLED'));
            };
            options.signal?.addEventListener('abort', onAbort, { once: true });
            const settle = (fn) => {
                options.signal?.removeEventListener('abort', onAbort);
                fn();
            };
            this.pending.set(id, {
                resolve: (value) => settle(() => resolvePromise(value)),
                reject: (error) => settle(() => rejectPromise(error)),
                timer,
            });
            try {
                child.stdin?.write(`${JSON.stringify({ id, op, args })}\n`);
            }
            catch (error) {
                this.pending.delete(id);
                clearTimeout(timer);
                settle(() => rejectPromise(new BridgeError(`cannot reach the dsh-cu bridge: ${String(error)}`, 'BRIDGE_WRITE_FAILED')));
            }
        });
    }
    /** True when the bridge answers a liveness probe. */
    async probe() {
        try {
            await this.request('status', {}, { timeoutMs: 30_000 });
            return true;
        }
        catch {
            return false;
        }
    }
    /** Ask the bridge to stop and close the child. */
    async dispose() {
        this.disposed = true;
        const child = this.child;
        if (child === undefined)
            return;
        try {
            await this.request('shutdown', {}, { timeoutMs: DISPOSE_GRACE_MS });
        }
        catch {
            // A bridge that cannot answer is torn down below anyway.
        }
        this.child = undefined;
        await new Promise((resolvePromise) => {
            if (child.exitCode !== null || child.killed) {
                resolvePromise();
                return;
            }
            const timer = setTimeout(() => {
                child.kill('SIGKILL');
                resolvePromise();
            }, DISPOSE_GRACE_MS);
            timer.unref?.();
            child.once('exit', () => {
                clearTimeout(timer);
                resolvePromise();
            });
            child.kill('SIGTERM');
        });
    }
}
/** True when a value is a non-null object. */
function isRecord(value) {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}
/** Drop keys whose value is undefined so the bridge sees only what was asked for. */
function defined(args) {
    const payload = {};
    for (const [key, value] of Object.entries(args)) {
        if (value !== undefined)
            payload[key] = value;
    }
    return payload;
}
/** Render one bridge result as digestible text, bounded so a tree cannot flood the context. */
export function renderResult(value, limit = 20_000) {
    let text;
    try {
        text = JSON.stringify(value, null, 2) ?? String(value);
    }
    catch {
        text = String(value);
    }
    if (text.length <= limit)
        return text;
    return `${text.slice(0, limit)}\n… truncated (${text.length - limit} more characters)`;
}
/** Describe a screenshot result in one line, since the image itself is a file. */
export function renderScreenshot(value) {
    if (!isRecord(value))
        return renderResult(value);
    const pixels = Array.isArray(value.pixels) ? value.pixels.join('x') : '?';
    const logical = isRecord(value.logical) ? value.logical : {};
    const origin = Array.isArray(logical.origin) ? logical.origin.join(',') : '?';
    const size = Array.isArray(logical.size) ? logical.size.join('x') : '?';
    const lines = [
        `Saved ${String(value.path)}`,
        `backend ${String(value.backend)}; ${pixels}px; logical origin (${origin}) size ${size}`,
        String(value.coordinateNote ?? ''),
    ];
    if (typeof value.reason === 'string' && value.reason.length > 0)
        lines.push(`warning: ${value.reason}`);
    if (value.sessionLocked === true) {
        lines.push('warning: the session is locked, so this frame shows the lock screen');
    }
    return lines.filter((line) => line.length > 0).join('\n');
}
/** Tool specifications, in the order they are registered. */
export const TOOL_SPECS = [
    {
        suffix: 'status',
        op: 'status',
        concurrencySafe: true,
        timeoutMs: 60_000,
        description: 'Report what this machine can do for computer use before relying on it: KWin session and output '
            + 'geometry, whether input injection (KWin EIS + libei) works, which screenshot backend is authorized, '
            + 'whether toolkit accessibility is on, whether the session is locked, Klipper, and the installed and '
            + 'running flatpak apps. Call this first when a computer-use task starts or after something failed.',
        parameters: {
            type: 'object',
            properties: {
                refreshScreenshotAuthorization: {
                    type: 'boolean',
                    description: 'Re-probe KWin ScreenShot2 authorization instead of using the cached probe.',
                },
            },
            additionalProperties: false,
        },
    },
    {
        suffix: 'windows',
        op: 'window.list',
        concurrencySafe: true,
        timeoutMs: 30_000,
        description: 'List windows from KWin with their exact geometry in global logical coordinates. Each entry carries '
            + 'the KWin window id (used by every other window tool), the app id, the pid, the frame/client/buffer '
            + 'rectangles, and flatpakAppId when the window belongs to a flatpak sandbox. This is the cheapest way '
            + 'to find what to act on, and the only one that sees apps that publish no accessibility tree.',
        parameters: {
            type: 'object',
            properties: {
                app: { type: 'string', description: 'Case-insensitive substring match on app id, caption, or resource name.' },
                caption: { type: 'string', description: 'Case-insensitive substring match on the window caption.' },
                includePopups: { type: 'boolean', description: 'Include popup windows (menus, tooltips). Default true.' },
                includeSpecial: {
                    type: 'boolean',
                    description: 'Include desktop, dock, and notification windows, which are filtered out by default.',
                },
            },
            additionalProperties: false,
        },
    },
    {
        suffix: 'window',
        op: 'window.control',
        concurrencySafe: false,
        mutating: true,
        timeoutMs: 30_000,
        description: 'Act on one window by KWin id (preferred) or app/caption substring: info, activate, close, minimize, '
            + 'maximize, fullscreen, keep_above, on_all_desktops, or geometry (move/resize). Activation goes through '
            + 'KWin because AT-SPI focus does not raise windows on Wayland, so activate before typing or clicking '
            + 'when the target may not be focused. Returns the window state before and after, and `refused` when the '
            + 'window does not allow the action.',
        parameters: {
            type: 'object',
            properties: {
                action: {
                    type: 'string',
                    required: true,
                    enum: ['info', 'activate', 'close', 'minimize', 'maximize', 'fullscreen', 'keep_above', 'on_all_desktops', 'geometry'],
                    description: 'What to do with the window.',
                },
                windowId: { type: 'string', description: 'Exact KWin window id, including braces, as reported by cu_windows.' },
                app: { type: 'string', description: 'App id or caption substring, used when windowId is omitted.' },
                value: {
                    type: 'boolean',
                    description: 'For minimize/maximize/fullscreen/keep_above/on_all_desktops: desired state. Default true.',
                },
                x: { type: 'integer', description: 'For geometry: new frame x in global logical coordinates.' },
                y: { type: 'integer', description: 'For geometry: new frame y in global logical coordinates.' },
                width: { type: 'integer', description: 'For geometry: new frame width.' },
                height: { type: 'integer', description: 'For geometry: new frame height.' },
            },
            additionalProperties: false,
        },
    },
    {
        suffix: 'screenshot',
        op: 'screenshot',
        concurrencySafe: true,
        timeoutMs: 90_000,
        description: 'Capture the workspace, one window, or one area and save it as a PNG, returning the path plus the '
            + 'logical origin and size of the frame so pixel coordinates can be converted to the global coordinates '
            + 'that cu_mouse takes. Read the saved file with the read_image tool to look at it. Prefers KWin '
            + 'ScreenShot2 and falls back to Spectacle; a uniform single-colour frame is reported as a warning '
            + 'because it means the display is off, the compositor is not rendering, or the session is locked.',
        parameters: {
            type: 'object',
            properties: {
                target: { type: 'string', enum: ['workspace', 'window', 'area'], description: 'What to capture. Default workspace.' },
                windowId: { type: 'string', description: 'For target=window: exact KWin window id.' },
                app: { type: 'string', description: 'For target=window: app id or caption substring instead of windowId.' },
                x: { type: 'integer', description: 'For target=area: left edge in global logical coordinates.' },
                y: { type: 'integer', description: 'For target=area: top edge in global logical coordinates.' },
                width: { type: 'integer', description: 'For target=area: width in logical pixels.' },
                height: { type: 'integer', description: 'For target=area: height in logical pixels.' },
                saveTo: { type: 'string', description: 'File or directory to write. Defaults to the plugin screenshot directory.' },
                maxEdge: { type: 'integer', description: 'Downscale so the longer edge is at most this many pixels; 0 keeps full size.' },
                includeCursor: { type: 'boolean', description: 'Draw the pointer into the capture.' },
                backend: {
                    type: 'string',
                    enum: ['auto', 'screenshot2', 'spectacle'],
                    description: 'Force one backend instead of trying ScreenShot2 and falling back to Spectacle.',
                },
                refreshAuthorization: { type: 'boolean', description: 'Re-probe ScreenShot2 authorization before capturing.' },
            },
            additionalProperties: false,
        },
    },
    {
        suffix: 'mouse',
        op: 'mouse',
        concurrencySafe: false,
        mutating: true,
        timeoutMs: 60_000,
        description: 'Move, click, drag, or scroll the pointer. Coordinates are global logical pixels, which is also the '
            + 'coordinate space of cu_windows, cu_a11y_find and cu_screenshot. Actions: move, click, double_click, '
            + 'triple_click, down, up, drag (needs x,y and toX,toY), scroll (amount in wheel steps), and position, '
            + 'which reads the current pointer position. Injection goes through the compositor, so it reaches '
            + 'Wayland, XWayland, Wine, and flatpak windows alike, but it lands on whichever window has focus: '
            + 'activate the target first.',
        parameters: {
            type: 'object',
            properties: {
                action: {
                    type: 'string',
                    required: true,
                    enum: ['move', 'click', 'double_click', 'triple_click', 'down', 'up', 'drag', 'scroll', 'position'],
                    description: 'Which pointer gesture to perform.',
                },
                x: { type: 'integer', description: 'Target x in global logical coordinates.' },
                y: { type: 'integer', description: 'Target y in global logical coordinates.' },
                toX: { type: 'integer', description: 'For drag: end x.' },
                toY: { type: 'integer', description: 'For drag: end y.' },
                button: { type: 'string', enum: ['left', 'right', 'middle'], description: 'Mouse button. Default left.' },
                clicks: { type: 'integer', description: 'Override the click count for action=click.' },
                holdMs: { type: 'integer', description: 'Hold the last click this many milliseconds (long press).' },
                amount: { type: 'integer', description: 'For scroll: wheel steps; positive scrolls up/left, negative down/right.' },
                horizontal: { type: 'boolean', description: 'For scroll: scroll horizontally instead of vertically.' },
                discrete: { type: 'boolean', description: 'For scroll: whole detents (default) rather than smooth pixels.' },
                steps: { type: 'integer', description: 'For drag: intermediate motion steps. Default 20.' },
            },
            additionalProperties: false,
        },
    },
    {
        suffix: 'keyboard',
        op: 'keyboard',
        concurrencySafe: false,
        mutating: true,
        timeoutMs: 120_000,
        description: 'Type text or press key chords on the focused window. action=type types literal text; action=press '
            + 'taps a chord such as "ctrl+alt+t" or "Return"; down/up hold and release one. Text is typed as '
            + 'US-QWERTY keycodes, so for characters outside that layout pass unicode=true, which puts the text on '
            + 'the clipboard and presses the paste chord (refused when the target has no known chord, because a '
            + 'wrong chord injects a literal ^V into a terminal). Activate the target window first.',
        parameters: {
            type: 'object',
            properties: {
                action: { type: 'string', required: true, enum: ['type', 'press', 'down', 'up'], description: 'What to do.' },
                text: { type: 'string', description: 'For action=type: the text to type.' },
                keys: {
                    type: 'string',
                    description: 'For press/down/up: a chord like "ctrl+shift+v", "Return", "F5", or a single character.',
                },
                unicode: {
                    type: 'boolean',
                    description: 'For action=type: use the clipboard-paste path even when the text is US-QWERTY typeable.',
                },
            },
            additionalProperties: false,
        },
    },
    {
        suffix: 'a11y_tree',
        op: 'a11y.tree',
        concurrencySafe: true,
        timeoutMs: 90_000,
        description: 'Walk the AT-SPI accessibility tree of one application (or all of them) with each element\'s rectangle '
            + 'translated into global logical coordinates. Elements whose window could not be matched with certainty '
            + 'report `unavailable` instead of a rectangle, and those coordinates must not be used for input. '
            + 'Prefer cu_a11y_find or cu_windows when you know what you are looking for; this is for exploring. '
            + 'Flutter, Wine, and some Electron apps publish no useful tree — fall back to cu_screenshot plus '
            + 'cu_mouse there.',
        parameters: {
            type: 'object',
            properties: {
                app: { type: 'string', description: 'Application name substring, for example "Brave" or "Flatseal".' },
                windowId: { type: 'string', description: 'Restrict the walk to the application owning this KWin window id.' },
                maxDepth: { type: 'integer', description: 'Maximum tree depth. Default 15.' },
                role: { type: 'string', description: 'Only return elements with this role, for example "push button".' },
                maxElements: { type: 'integer', description: 'Cap the returned elements. Default 400.' },
                ensureEnabled: {
                    type: 'boolean',
                    description: 'Turn toolkit accessibility on when it is off (default true); apps may need a restart to register.',
                },
            },
            additionalProperties: false,
        },
    },
    {
        suffix: 'a11y_find',
        op: 'a11y.find',
        concurrencySafe: true,
        timeoutMs: 90_000,
        description: 'Find accessibility elements whose name, role, or description contains a query, with global logical '
            + 'coordinates and the actions each element supports. This is the reliable way to locate a control to '
            + 'click: pass waitMs to poll while a window finishes opening. Combine with cu_a11y_invoke to act on an '
            + 'element directly instead of clicking coordinates.',
        parameters: {
            type: 'object',
            properties: {
                query: { type: 'string', description: 'Case-insensitive substring of the element name, role, or description. Empty matches everything.' },
                app: { type: 'string', description: 'Application name substring.' },
                windowId: { type: 'string', description: 'Restrict the search to the application owning this KWin window id.' },
                states: {
                    type: 'array',
                    items: { type: 'string' },
                    description: 'Only match elements that have all of these AT-SPI states, for example ["showing", "enabled"].',
                },
                waitMs: { type: 'integer', description: 'Poll for up to this long before giving up. Default 0 (one pass).' },
                maxResults: { type: 'integer', description: 'Cap the returned matches. Default 50.' },
            },
            additionalProperties: false,
        },
    },
    {
        suffix: 'a11y_invoke',
        op: 'a11y.invoke',
        concurrencySafe: false,
        mutating: true,
        timeoutMs: 90_000,
        description: 'Run one accessibility action on exactly one matching element — the most reliable way to press a button, '
            + 'toggle, or menu item, and the only one that works when the element has no usable coordinates. Refuses '
            + 'when more than one element matches (pass index to choose) or when the subtree cannot be read completely, '
            + 'because acting on the wrong control cannot be undone. Use action="click" for the default action.',
        parameters: {
            type: 'object',
            properties: {
                query: { type: 'string', required: true, description: 'Element name, role, or description substring identifying the target.' },
                app: { type: 'string', description: 'Application name substring to search within.' },
                windowId: { type: 'string', description: 'Restrict the search to the application owning this KWin window id.' },
                action: { type: 'string', description: 'Exact AT-SPI action name; "click" (the default) picks click/activate/press/toggle.' },
                index: { type: 'integer', description: 'Pick this match when several match, instead of refusing.' },
            },
            additionalProperties: false,
        },
    },
    {
        suffix: 'a11y_windows',
        op: 'a11y.windows',
        concurrencySafe: true,
        timeoutMs: 60_000,
        description: 'List the applications and top-level windows that AT-SPI knows about, with each application\'s pid and '
            + 'flatpakAppId. Use it to discover which apps can be driven through accessibility at all, and to see '
            + 'the proxyPid of a flatpak app, whose accessibility tree is published by its sandbox proxy rather than '
            + 'by the app process itself.',
        parameters: { type: 'object', properties: {}, additionalProperties: false },
    },
    {
        suffix: 'launch',
        op: 'app.launch',
        concurrencySafe: false,
        mutating: true,
        timeoutMs: 120_000,
        description: 'Launch an app and wait for its window, then report both. For a flatpak app pass appId (for example '
            + '"com.github.tchx84.Flatseal"); the window is matched through the instance\'s app-flatpak cgroup scope, '
            + 'which is exact even with several instances running. For a host program pass command and args. The '
            + 'wait is part of the call on purpose: polling for a window yourself usually ends up activating whatever '
            + 'was already focused.',
        parameters: {
            type: 'object',
            properties: {
                appId: { type: 'string', description: 'Flatpak application id to run.' },
                command: { type: 'string', description: 'Host program to run, when appId is not used.' },
                args: { type: 'array', items: { type: 'string' }, description: 'Arguments for command, or for the flatpak app.' },
                env: {
                    type: 'object',
                    additionalProperties: { type: 'string' },
                    description: 'Environment variables for the launched app (flatpak passes these with --env=K=V).',
                },
                branch: { type: 'string', description: 'Flatpak branch, for example "stable".' },
                flatpakCommand: { type: 'string', description: 'Run this command inside the sandbox instead of the app\'s own entry point.' },
                windowMatch: { type: 'string', description: 'Extra fallback match on app id or caption for the window to wait for.' },
                waitMs: { type: 'integer', description: 'How long to wait for the window. Default 20000.' },
            },
            additionalProperties: false,
        },
    },
    {
        suffix: 'flatpak',
        op: 'flatpak.info',
        concurrencySafe: true,
        timeoutMs: 60_000,
        description: 'List installed flatpak apps and running instances, or describe one app: its permissions (including '
            + 'whether the sandbox may talk to the accessibility bus, which is what makes its widget tree visible), '
            + 'its running instances with pids and cgroup scopes, and the current accessibility bus state. Use this '
            + 'when a flatpak app cannot be inspected or driven, to see whether the sandbox or accessibility is the '
            + 'obstacle.',
        parameters: {
            type: 'object',
            properties: {
                appId: { type: 'string', description: 'Describe this one app instead of listing everything.' },
            },
            additionalProperties: false,
        },
    },
    {
        suffix: 'clipboard',
        op: 'clipboard',
        concurrencySafe: false,
        mutating: true,
        timeoutMs: 30_000,
        description: 'Read or write the clipboard through Plasma\'s Klipper. Writing is the escape hatch for getting text '
            + 'into an app when typing it is impractical, and reading is how you capture what a user copied.',
        parameters: {
            type: 'object',
            properties: {
                action: { type: 'string', required: true, enum: ['read', 'write'], description: 'Read the clipboard or replace it.' },
                text: { type: 'string', description: 'For action=write: the text to place on the clipboard.' },
            },
            additionalProperties: false,
        },
    },
];
/**
 * Build one registry-ready tool definition from a specification.
 * @param spec - the tool specification.
 * @param client - the resident bridge client.
 * @param prefix - tool-name prefix.
 * @param launchTimeoutMs - budget for the launching tool.
 * @returns the definition to register.
 */
export function buildDefinition(spec, client, prefix, launchTimeoutMs) {
    const timeoutMs = spec.suffix === 'launch' ? launchTimeoutMs : spec.timeoutMs;
    return {
        name: `${prefix}${spec.suffix}`,
        description: spec.description,
        parameters: spec.parameters,
        output: {
            schema: {},
            render: (_args, value) => [
                {
                    type: 'text',
                    text: spec.suffix === 'screenshot' ? renderScreenshot(value) : renderResult(value),
                },
            ],
        },
        ...timeoutMs === undefined ? {} : { timeoutMs },
        isConcurrencySafe: () => spec.concurrencySafe,
        async execute(args, exec) {
            const payload = spec.toPayload === undefined ? defined(args) : spec.toPayload(defined(args));
            return await client.request(spec.op, payload, { signal: exec.signal, timeoutMs });
        },
    };
}
/**
 * Register the computer-use tool surface.
 * @param ctx - plugin context; `ctx.tools` must be mounted.
 * @param config - optional plugin configuration.
 * @returns how many tools were registered.
 */
export function apply(ctx, config = {}) {
    const resolved = resolveConfig(config);
    const launcher = launcherPath(resolved);
    const client = new BridgeClient(resolved, launcher);
    const disposers = [];
    for (const spec of TOOL_SPECS) {
        if (spec.mutating === true && !resolved.enableInput)
            continue;
        const definition = buildDefinition(spec, client, resolved.toolPrefix, resolved.launchTimeoutMs);
        disposers.push(ctx.tools.register(definition));
    }
    const skill = resolved.enableSkill ? skillRegistration(ctx, resolved) : undefined;
    if (skill !== undefined)
        disposers.push(skill);
    ctx.on?.('dispose', () => {
        for (const dispose of disposers.splice(0))
            dispose();
        void client.dispose();
    });
    const startup = `dsh-cu: registered ${String(disposers.length)} computer-use entries `
        + `(prefix "${resolved.toolPrefix}", bridge ${launcher})`;
    ctx.logger?.info(startup);
    // Also on stderr: a host that wires no log sink to `ctx.logger` would
    // otherwise swallow the only evidence that the tools mounted.
    console.error(startup);
    return disposers.length;
}
/** The skill body: the loop that keeps an agent out of the usual traps. */
export const SKILL_CONTENT = `# Plasma computer use

Drive a KDE Plasma 6 Wayland desktop: windows, screenshots, pointer, keyboard,
accessibility, flatpak apps.

## The loop

1. \`cu_status\` — confirm KWin, input injection, screenshots, accessibility, and
   whether the session is locked. Do this once at the start and after any
   failure; each capability fails on its own.
2. \`cu_windows\` — find the target and take its KWin window id and geometry.
3. \`cu_window\` with \`action: "activate"\` — focus it. Synthetic input lands on
   whichever window has focus, so this is not optional.
4. Observe: \`cu_screenshot\` (read the PNG back with \`read_image\`), or
   \`cu_a11y_find\` when the app publishes a widget tree.
5. Act: \`cu_a11y_invoke\` for a named control, otherwise \`cu_mouse\`/\`cu_keyboard\`
   at coordinates.
6. Verify: screenshot or accessibility state again. Never assume a click landed.

## Coordinates

Everything is global logical pixels: \`cu_windows\` rectangles, \`cu_a11y_find\`
rectangles, \`cu_mouse\` targets. A screenshot is a window onto that same space:
its result states the logical origin and size, and downscaled images carry the
formula. Elements with \`unavailable\` set have no trustworthy rectangle — do not
click them.

## Flatpak

Flatpak is not a special case for input or screenshots: KWin composites the
window and the compositor-level injection reaches the sandbox. It does matter for
identity and accessibility:

* \`cu_windows\` reports \`flatpakAppId\` for sandboxed windows.
* A flatpak app's accessibility tree is published by its sandbox's
  \`xdg-dbus-proxy\`, so AT-SPI reports the proxy's pid; \`cu_a11y_windows\` shows
  both \`proxyPid\` and \`flatpakAppId\` so the two views line up.
* Launch with \`cu_launch { appId }\`, which waits for the window belonging to that
  app's cgroup scope.
* If a flatpak app shows no widget tree, check \`cu_flatpak { appId }\`: the
  sandbox may lack \`org.a11y.Bus\`, or toolkit accessibility may be off
  (the accessibility tools turn it on, but the app may need a restart).

## Traps

* **Locked session.** \`cu_status\` reports it. Screenshots show the lock screen
  and input lands on it; unlock before driving anything.
* **Blank frames.** A uniform single-colour screenshot means the display is
  powered off, the compositor is not rendering, or the session is locked — the
  result says which when it can. Synthetic input usually wakes the display.
* **US-QWERTY typing.** \`cu_keyboard\` types US-QWERTY keycodes. For other
  characters use \`unicode: true\`, which pastes via the clipboard and restores it.
* **Terminals.** The clipboard path refuses to paste into a terminal whose paste
  chord is unknown, because a wrong chord sends \`^V\` to the shell.
* **No tree.** Wine, Flutter, and some Electron apps publish nothing usable;
  fall back to screenshot plus coordinates.
`;
/**
 * Register the bundled usage skill when the deployment provides `ctx.skills`.
 * @param ctx - plugin context.
 * @param config - resolved configuration.
 * @returns a disposer, or undefined when skills are unavailable.
 */
function skillRegistration(ctx, config) {
    const skills = ctx.get?.('skills');
    if (skills?.register === undefined)
        return undefined;
    return skills.register({
        name: 'plasma-computer-use',
        description: 'Drive a KDE Plasma 6 Wayland desktop with the cu_ tools: find and activate windows, take verifiable '
            + 'screenshots, inject pointer and keyboard input, use the AT-SPI accessibility tree, and launch or '
            + 'inspect flatpak apps.',
        whenToUse: 'Use for any request to operate the Linux desktop: open or drive a GUI app, click or type into one, '
            + 'take a screenshot, inspect a window or its widgets, or work with a flatpak app.',
        content: SKILL_CONTENT,
        invocation: { modelInvocable: true, userInvocable: true },
    });
}
