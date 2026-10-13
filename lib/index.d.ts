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
/** Cordis plugin name used by loader diagnostics. */
export declare const name = "computer-use-plasma";
/** The tool registry is the one hard dependency; the bridge is loaded from config. */
export declare const inject: string[];
/** Default interpreter used to launch the bridge. */
export declare const DEFAULT_PYTHON = "python3";
/** Relative path of the bridge launcher inside this package. */
export declare const LAUNCHER_RELATIVE_PATH = "bin/dsh-cu-bridge";
/** Package specifier of the harness tools module, used only to validate schemas. */
export declare const TOOLS_SPECIFIER = "@deepseek-ai/dsh-tools";
/** Default prefix every registered tool name starts with. */
export declare const DEFAULT_TOOL_PREFIX = "cu_";
/** Default per-request budget for a bridge operation (ms). */
export declare const DEFAULT_REQUEST_TIMEOUT_MS = 60000;
/** Default budget for `cu_launch`, which waits for a window to appear (ms). */
export declare const DEFAULT_LAUNCH_TIMEOUT_MS = 20000;
/** Default long-edge bound for screenshots; 0 keeps the capture at full size. */
export declare const DEFAULT_SCREENSHOT_MAX_EDGE = 0;
/** How long a disposed bridge gets to exit on its own before it is killed (ms). */
export declare const DISPOSE_GRACE_MS = 2000;
/**
 * Plugin configuration. Every field is optional; the environment fills the gaps
 * (`DSH_CU_PYTHON`, `DSH_CU_SCRIPT_DIR`, `DSH_CU_STATE_DIR`,
 * `DSH_CU_SCREENSHOT_DIR`).
 */
export interface Config {
    /** Interpreter used to run the bridge. Default `python3`. */
    python?: string;
    /** Absolute path to the launcher, when it was moved out of this package. */
    bridgePath?: string;
    /**
     * Directory KWin can read for the temporary scripts window control needs.
     * Set this when the harness runs with a private `/tmp` (a sandbox), because
     * KWin is a different process and cannot see a private mount.
     */
    scriptDir?: string;
    /** Directory for bridge logs and the default screenshot location. */
    stateDir?: string;
    /** Directory screenshots are written to when a call does not name one. */
    screenshotDir?: string;
    /** Per-request budget in milliseconds. Default 60000. */
    requestTimeoutMs?: number;
    /** Budget for launching an app and waiting for its window. Default 20000. */
    launchTimeoutMs?: number;
    /** Prefix for every tool name. Default `cu_`. */
    toolPrefix?: string;
    /**
     * Register the input, launch, and clipboard tools. Default true. Set false to
     * expose an observation-only surface (windows, screenshots, accessibility).
     */
    enableInput?: boolean;
    /** Register the bundled skill that explains the observe/act/verify loop. Default true. */
    enableSkill?: boolean;
    /** Extra environment variables for the bridge, merged last. */
    env?: Record<string, string>;
}
/** The subset of the Cordis context this plugin uses. */
export interface PluginContext {
    tools: {
        register(definition: unknown): () => void;
    };
    get?(service: string): unknown;
    logger?: {
        info(message: string, ...rest: unknown[]): void;
        warn(message: string, ...rest: unknown[]): void;
        error(message: string, ...rest: unknown[]): void;
    };
    on?(event: string, listener: (...args: unknown[]) => void): () => void;
}
/** Resolved configuration, with every default applied and every type checked. */
export interface ResolvedConfig {
    python: string;
    bridgePath: string | undefined;
    scriptDir: string | undefined;
    stateDir: string | undefined;
    screenshotDir: string | undefined;
    requestTimeoutMs: number;
    launchTimeoutMs: number;
    toolPrefix: string;
    enableInput: boolean;
    enableSkill: boolean;
    env: Record<string, string>;
}
/** A transport- or operation-level failure, carrying the bridge's own code. */
export declare class BridgeError extends Error {
    /** Machine-routable code from the bridge (`NO_SUCH_WINDOW`, `EIS_UNAVAILABLE`, ...). */
    readonly code: string;
    /** Optional structured detail from the bridge. */
    readonly detail: unknown;
    /**
     * @param message - human-readable failure.
     * @param code - machine-routable code.
     * @param detail - optional structured detail.
     */
    constructor(message: string, code?: string, detail?: unknown);
}
/**
 * Apply defaults and validate one plugin config object.
 * @param config - raw plugin config, possibly empty.
 * @returns the resolved configuration.
 */
export declare function resolveConfig(config?: Config): ResolvedConfig;
/**
 * Walk up from a module URL until the `package.json` naming this package is
 * found, so the plugin works both from `lib/` and straight from `index.ts`.
 * @param startDir - directory of the running module.
 * @returns the package root, or the start directory when no marker is found.
 */
export declare function findPackageRoot(startDir: string): string;
/** Directory of the running module. */
export declare function moduleDir(): string;
/** This package's root directory, resolved from the running module. */
export declare function packageRoot(): string;
/**
 * Resolve the launcher path for this installation.
 * @param config - resolved configuration.
 * @returns the absolute launcher path.
 */
export declare function launcherPath(config: ResolvedConfig): string;
/** One newline-delimited JSON-RPC client over a resident bridge process. */
export declare class BridgeClient {
    private readonly config;
    private readonly launcher;
    private child;
    private buffer;
    private nextId;
    private readonly pending;
    private disposed;
    /**
     * @param config - resolved plugin configuration.
     * @param launcher - absolute path of the bridge launcher.
     */
    constructor(config: ResolvedConfig, launcher: string);
    /** Whether the bridge process is currently running. */
    get running(): boolean;
    /** Environment for the bridge child: the session plus every configured override. */
    private childEnv;
    /** Start the bridge process when it is not already running. */
    private ensureChild;
    /** Handle one chunk of the response stream, framing on newlines. */
    private onData;
    /** Resolve or reject one response frame. */
    private onFrame;
    /** Reject every in-flight request when the child disappears. */
    private onExit;
    /** Reject every pending request with one error. */
    private failAll;
    /**
     * Run one operation on the bridge.
     * @param op - bridge operation name (`window.list`, `mouse`, ...).
     * @param args - operation arguments.
     * @param options - per-call signal and timeout budget.
     * @returns the operation result as parsed JSON.
     */
    request(op: string, args: Record<string, unknown>, options?: {
        signal?: AbortSignal;
        timeoutMs?: number;
    }): Promise<unknown>;
    /** True when the bridge answers a liveness probe. */
    probe(): Promise<boolean>;
    /** Ask the bridge to stop and close the child. */
    dispose(): Promise<void>;
}
/** One registered tool: its schema, its bridge operation, and its behaviour. */
interface ToolSpec {
    /** Tool name without the configured prefix. */
    suffix: string;
    /** Model-facing description. */
    description: string;
    /** Bridge operation name. */
    op: string;
    /** Raw JSON Schema for the arguments (the registry's own contract). */
    parameters: Record<string, unknown>;
    /** Cooperative timeout budget in milliseconds. */
    timeoutMs?: number;
    /** Whether the call is marked safe to overlap with sibling calls. */
    concurrencySafe: boolean;
    /** Whether the tool mutates the desktop (gated by `enableInput`). */
    mutating?: boolean;
    /** Fold model arguments into the bridge payload. Defaults to identity. */
    toPayload?(args: Record<string, unknown>): Record<string, unknown>;
}
/** One author-facing parameter: JSON Schema keywords plus the DSL's `required` flag. */
export type PropertySpec = Record<string, unknown> & {
    required?: true;
};
/**
 * Compile author-facing parameter specs into raw JSON Schema.
 *
 * The distinction this function exists for is easy to get wrong and expensive
 * when you do: the `defineTool` authoring DSL marks a property required *inside*
 * the property (`{ type: 'string', required: true }`), while the schema that
 * actually reaches the model provider is raw JSON Schema, where `required` is an
 * **array of property names on the object**. Sending the DSL form makes the
 * provider reject every request — "True is not of type 'array'" — because tool
 * schemas ship with every call, so one bad schema breaks all of them.
 *
 * Nested object properties are written as raw JSON Schema already (their own
 * `required` arrays are untouched); only the top level of the parameter map uses
 * the flag.
 *
 * @param properties - author-facing parameter specs.
 * @returns the object-rooted raw schema, with a root `required` array.
 */
export declare function compileParameters(properties: Record<string, PropertySpec>): Record<string, unknown>;
/**
 * Check one parameter schema against the rules that matter for this plugin.
 *
 * Dependency-free on purpose: this runs before every registration, so the guard
 * cannot be missing on a host where the harness's own validator cannot be
 * resolved. It covers exactly the ways a schema here can be malformed —
 * property-level `required`, a `required` array naming undeclared properties, an
 * unsupported `additionalProperties`, an array without `items`, an empty `enum`.
 *
 * @param tool - tool name, for the message.
 * @param schema - the compiled parameter schema.
 * @returns one message per problem; empty means the schema is well formed.
 */
export declare function structuralSchemaProblems(tool: string, schema: Record<string, unknown>): string[];
/** The harness's own schema validator, when it can be resolved. */
interface HarnessValidator {
    assertSupportedJsonSchema?(schema: unknown): void;
}
/**
 * Resolve the harness's schema validator.
 *
 * A linked or installed out-of-tree plugin has no `node_modules` of its own, so
 * the bare specifier only resolves when the profile happens to provide it; the
 * fallback resolves the harness's own copy from the running entry point, which
 * is the same physical module the harness loads. `DSH_CU_TOOLS_ENTRY` overrides
 * the specifier or absolute path.
 *
 * @returns the validator module, or undefined when nothing resolved.
 */
export declare function loadHarnessValidator(): Promise<HarnessValidator | undefined>;
/**
 * Validate every tool spec before anything is registered.
 *
 * A malformed parameter schema is not a local problem: tool schemas are sent to
 * the model provider on **every** request, so one bad schema makes every turn
 * fail. The harness accepts plugin parameters without checking them, so the
 * check has to happen here.
 *
 * @param validator - the harness validator, when it could be resolved.
 * @returns one message per problem across every spec; empty means all are valid.
 */
export declare function validateToolSpecs(validator?: HarnessValidator): string[];
/** Render one bridge result as digestible text, bounded so a tree cannot flood the context. */
export declare function renderResult(value: unknown, limit?: number): string;
/** Describe a screenshot result in one line, since the image itself is a file. */
export declare function renderScreenshot(value: unknown): string;
/** Tool specifications, in the order they are registered. */
export declare const TOOL_SPECS: readonly ToolSpec[];
/**
 * Build one registry-ready tool definition from a specification.
 * @param spec - the tool specification.
 * @param client - the resident bridge client.
 * @param prefix - tool-name prefix.
 * @param launchTimeoutMs - budget for the launching tool.
 * @returns the definition to register.
 */
export declare function buildDefinition(spec: ToolSpec, client: BridgeClient, prefix: string, launchTimeoutMs: number): unknown;
/**
 * Register the computer-use tool surface.
 *
 * Every schema is validated before anything is registered. Tool schemas travel
 * with every model request, so an invalid one does not degrade this plugin — it
 * makes **every** turn fail with a provider-side 400. A spec that fails
 * validation is therefore skipped with a loud message instead of being
 * registered, which keeps the rest of the harness usable.
 *
 * Returning nothing is deliberate: an async plugin entry point may only resolve
 * to an effect, and a count is not one (Cordis rejects it with "Invalid effect").
 *
 * @param ctx - plugin context; `ctx.tools` must be mounted.
 * @param config - optional plugin configuration.
 */
export declare function apply(ctx: PluginContext, config?: Config): Promise<void>;
/** The skill body: the loop that keeps an agent out of the usual traps. */
export declare const SKILL_CONTENT = "# Plasma computer use\n\nDrive a KDE Plasma 6 Wayland desktop: windows, screenshots, pointer, keyboard,\naccessibility, flatpak apps.\n\n## The loop\n\n1. `cu_status` \u2014 confirm KWin, input injection, screenshots, accessibility, and\n   whether the session is locked. Do this once at the start and after any\n   failure; each capability fails on its own.\n2. `cu_windows` \u2014 find the target and take its KWin window id and geometry.\n3. `cu_window` with `action: \"activate\"` \u2014 focus it. Synthetic input lands on\n   whichever window has focus, so this is not optional.\n4. Observe: `cu_screenshot` (read the PNG back with `read_image`), or\n   `cu_a11y_find` when the app publishes a widget tree.\n5. Act: `cu_a11y_invoke` for a named control, otherwise `cu_mouse`/`cu_keyboard`\n   at coordinates.\n6. Verify: screenshot or accessibility state again. Never assume a click landed.\n\n## Coordinates\n\nEverything is global logical pixels: `cu_windows` rectangles, `cu_a11y_find`\nrectangles, `cu_mouse` targets. A screenshot is a window onto that same space:\nits result states the logical origin and size, and downscaled images carry the\nformula. Elements with `unavailable` set have no trustworthy rectangle \u2014 do not\nclick them.\n\n## Flatpak\n\nFlatpak is not a special case for input or screenshots: KWin composites the\nwindow and the compositor-level injection reaches the sandbox. It does matter for\nidentity and accessibility:\n\n* `cu_windows` reports `flatpakAppId` for sandboxed windows.\n* A flatpak app's accessibility tree is published by its sandbox's\n  `xdg-dbus-proxy`, so AT-SPI reports the proxy's pid; `cu_a11y_windows` shows\n  both `proxyPid` and `flatpakAppId` so the two views line up.\n* Launch with `cu_launch { appId }`, which waits for the window belonging to that\n  app's cgroup scope.\n* If a flatpak app shows no widget tree, check `cu_flatpak { appId }`: the\n  sandbox may lack `org.a11y.Bus`, or toolkit accessibility may be off\n  (the accessibility tools turn it on, but the app may need a restart).\n\n## Traps\n\n* **Locked session.** `cu_status` reports it. Screenshots show the lock screen\n  and input lands on it; unlock before driving anything.\n* **Blank frames.** A uniform single-colour screenshot means the display is\n  powered off, the compositor is not rendering, or the session is locked \u2014 the\n  result says which when it can. Synthetic input usually wakes the display.\n* **US-QWERTY typing.** `cu_keyboard` types US-QWERTY keycodes. For other\n  characters use `unicode: true`, which pastes via the clipboard and restores it.\n* **Terminals.** The clipboard path refuses to paste into a terminal whose paste\n  chord is unknown, because a wrong chord sends `^V` to the shell.\n* **No tree.** Wine, Flutter, and some Electron apps publish nothing usable;\n  fall back to screenshot plus coordinates.\n";
export {};
