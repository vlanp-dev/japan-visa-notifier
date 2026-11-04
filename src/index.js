#!/usr/bin/env node
import { createService } from './server.js';

const service = createService();

let shuttingDown = false;

async function shutdown(signal) {
  if (shuttingDown) {
    return;
  }
  shuttingDown = true;
  service.logger.info('service_stopping', { signal });
  try {
    await service.close();
    process.exit(0);
  } catch (error) {
    service.logger.error('service_stop_failed', { error });
    process.exit(1);
  }
}

process.on('SIGINT', () => {
  void shutdown('SIGINT');
});
process.on('SIGTERM', () => {
  void shutdown('SIGTERM');
});

try {
  const address = await service.listen();
  service.logger.info('service_started', {
    appEnv: service.config.appEnv,
    host: service.config.host,
    port: typeof address === 'object' && address !== null ? address.port : service.config.port,
  });
} catch (error) {
  service.logger.error('service_start_failed', { error });
  try {
    await service.close();
  } catch {
    // Nothing left to close.
  }
  process.exit(1);
}
