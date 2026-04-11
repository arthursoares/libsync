/**
 * @param {unknown} payload
 * @param {string} fallback
 */
export function getApiErrorMessage(payload, fallback) {
  if (payload && typeof payload === 'object') {
    const objectPayload = /** @type {{ detail?: unknown, error?: unknown, message?: unknown }} */ (payload);
    if (typeof objectPayload.detail === 'string' && objectPayload.detail) {
      return objectPayload.detail;
    }
    if (typeof objectPayload.error === 'string' && objectPayload.error) {
      return objectPayload.error;
    }
    if (typeof objectPayload.message === 'string' && objectPayload.message) {
      return objectPayload.message;
    }
  }

  if (typeof payload === 'string' && payload) {
    return payload;
  }

  return fallback;
}

/**
 * @param {Response} response
 * @param {string} fallback
 */
export async function readApiErrorMessage(response, fallback) {
  const contentType = response.headers.get('content-type') || '';

  if (contentType.includes('application/json')) {
    const payload = await response.json().catch(() => ({}));
    return getApiErrorMessage(payload, fallback);
  }

  const text = await response.text().catch(() => '');
  return getApiErrorMessage(text, fallback);
}
