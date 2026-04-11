/**
 * PluginRuntime store.
 *
 * External plugins must use api.runtime (PluginRuntime) from register(),
 * NOT ctx.runtime (RuntimeEnv) from startAccount().
 * PluginRuntime carries the channel-reply dispatch layer that routes
 * agent replies through the correct outbound adapter.
 *
 * Pattern from: openclaw-nostr plugin (https://github.com/k0sti/openclaw-nostr)
 */

let _runtime: any = null;

/** Store PluginRuntime at register time. */
export function setPluginRuntime(runtime: any): void {
  _runtime = runtime;
}

/** Retrieve PluginRuntime (call from startAccount). */
export function getPluginRuntime(): any {
  if (!_runtime) {
    throw new Error("[agentrux] PluginRuntime not initialized — setPluginRuntime() must be called in register()");
  }
  return _runtime;
}
