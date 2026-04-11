/**
 * @param {Array<{source?: string, authenticated?: boolean}> | null | undefined} statuses
 * @param {string} source
 */
export function getSourceAuth(statuses, source) {
  if (!Array.isArray(statuses)) {
    return null;
  }

  return statuses.find((status) => status?.source === source) ?? null;
}

/**
 * @param {Array<{source?: string, authenticated?: boolean}> | null | undefined} statuses
 * @param {string} source
 */
export function isSourceAuthenticated(statuses, source) {
  return Boolean(getSourceAuth(statuses, source)?.authenticated);
}

/**
 * @param {unknown} payload
 * @param {string} fallback
 */
export function getApiErrorDetail(payload, fallback) {
  if (payload && typeof payload === 'object') {
    const objectPayload = /** @type {{ detail?: unknown, error?: unknown }} */ (payload);
    if (typeof objectPayload.detail === 'string' && objectPayload.detail) {
      return objectPayload.detail;
    }
    if (typeof objectPayload.error === 'string' && objectPayload.error) {
      return objectPayload.error;
    }
  }

  return fallback;
}
