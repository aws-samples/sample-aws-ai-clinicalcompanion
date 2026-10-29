/**
 * ClinicalCompass Frontend Configuration — EXAMPLE
 * Copy this file to config.js and fill in your values.
 * Or run: python deployment/scripts/post_cdk_setup.py (auto-fills from CDK outputs)
 */

const config = {
  // WebSocket API endpoint
  websocketUrl: 'wss://YOUR_API_ID.execute-api.us-east-1.amazonaws.com/production',

  // Cognito authentication
  cognitoUserPoolId: 'YOUR_USER_POOL_ID',
  cognitoClientId: 'YOUR_CLIENT_ID',
  cognitoRegion: 'us-east-1',

  // Feature flags
  useWebSocket: true,

  // Connection settings
  maxReconnectAttempts: 5,
  reconnectDelay: 1000,
  pingIntervalMs: 30000,

  // UI settings
  autoScroll: true,
  showTimestamps: true,
  maxResponseLength: 50000
};

if (typeof module !== 'undefined' && module.exports) {
  module.exports = config;
}
