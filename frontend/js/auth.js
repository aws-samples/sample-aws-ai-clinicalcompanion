/**
 * ClinicalCompass Auth — Cognito authentication using raw API (no SDK needed)
 *
 * Uses the Cognito InitiateAuth API directly via fetch.
 * Stores tokens in sessionStorage (cleared on tab close).
 */

class ClinicalCompassAuth {
  constructor(config) {
    this.userPoolId = config.cognitoUserPoolId;
    this.clientId = config.cognitoClientId;
    this.region = config.cognitoRegion || 'us-east-1';
    this.endpoint = `https://cognito-idp.${this.region}.amazonaws.com/`;

    this.onAuthStateChange = null; // callback(isAuthenticated, user)
  }

  /**
   * Sign in with username and password
   * @returns {Promise<{success: boolean, error?: string, tokens?: object}>}
   */
  async signIn(username, password) {
    try {
      const response = await fetch(this.endpoint, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-amz-json-1.1',
          'X-Amz-Target': 'AWSCognitoIdentityProviderService.InitiateAuth',
        },
        body: JSON.stringify({
          AuthFlow: 'USER_PASSWORD_AUTH',
          ClientId: this.clientId,
          AuthParameters: {
            USERNAME: username,
            PASSWORD: password,
          },
        }),
      });

      const data = await response.json();

      if (data.AuthenticationResult) {
        const tokens = {
          idToken: data.AuthenticationResult.IdToken,
          accessToken: data.AuthenticationResult.AccessToken,
          refreshToken: data.AuthenticationResult.RefreshToken,
          expiresIn: data.AuthenticationResult.ExpiresIn,
          tokenType: data.AuthenticationResult.TokenType,
        };

        this._storeTokens(tokens);
        this._storeUser(username);

        if (this.onAuthStateChange) this.onAuthStateChange(true, username);
        return { success: true, tokens };
      }

      // Handle challenges (e.g., NEW_PASSWORD_REQUIRED)
      if (data.ChallengeName === 'NEW_PASSWORD_REQUIRED') {
        return { success: false, error: 'Password change required. Contact administrator.' };
      }

      // Handle errors
      const errorMsg = data.message || data.__type || 'Authentication failed';
      return { success: false, error: errorMsg };

    } catch (err) {
      return { success: false, error: err.message || 'Network error' };
    }
  }

  /**
   * Sign out — clear tokens
   */
  signOut() {
    sessionStorage.removeItem('cc_tokens');
    sessionStorage.removeItem('cc_user');
    if (this.onAuthStateChange) this.onAuthStateChange(false, null);
  }

  /**
   * Check if user is authenticated (has valid tokens)
   */
  isAuthenticated() {
    const tokens = this.getTokens();
    if (!tokens || !tokens.idToken) return false;

    // Check if token is expired (decode JWT payload)
    try {
      const payload = JSON.parse(atob(tokens.idToken.split('.')[1]));
      return payload.exp * 1000 > Date.now();
    } catch {
      return false;
    }
  }

  /**
   * Get stored tokens
   */
  getTokens() {
    try {
      return JSON.parse(sessionStorage.getItem('cc_tokens'));
    } catch {
      return null;
    }
  }

  /**
   * Get the ID token (for API authorization)
   */
  getIdToken() {
    const tokens = this.getTokens();
    return tokens ? tokens.idToken : null;
  }

  /**
   * Get the access token (preferred for service-backed authorization checks)
   */
  getAccessToken() {
    const tokens = this.getTokens();
    return tokens ? tokens.accessToken : null;
  }

  /**
   * Get current username
   */
  getUser() {
    return sessionStorage.getItem('cc_user');
  }

  // ── Internal ──────────────────────────────────────────────

  _storeTokens(tokens) {
    sessionStorage.setItem('cc_tokens', JSON.stringify(tokens));
  }

  _storeUser(username) {
    sessionStorage.setItem('cc_user', username);
  }
}
