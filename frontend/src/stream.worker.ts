/// <reference lib="webworker" />
self.onmessage = (event: MessageEvent<string>) => {
  try {
    const message = JSON.parse(event.data)
    if (message && typeof message.type === 'string') self.postMessage(message)
  } catch { /* discard malformed feed message */ }
}
