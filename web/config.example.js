// config.example.js — committed template. Copy to config.js at deploy time and
// fill from stack outputs. config.js is git-ignored and holds ONLY non-secret
// values (never credentials, account ids or ARNs).
//
// For a pure mock-mode review you do not need config.js at all: open the page
// with ?mock=1 and the UI runs entirely against mock.js with no network calls.

window.APP_CONFIG = {
  // --- Backend wiring (filled at deploy time) ---
  API_BASE_URL: "REPLACE_ME_api_base_url", // e.g. https://xxxx.execute-api.<region>.amazonaws.com
  COGNITO_DOMAIN: "REPLACE_ME_cognito_domain", // e.g. https://your-domain.auth.<region>.amazoncognito.com
  COGNITO_USER_POOL_ID: "REPLACE_ME_user_pool_id",
  COGNITO_APP_CLIENT_ID: "REPLACE_ME_app_client_id", // Cognito "app client" id (technical use of the word client)
  REGION: "REPLACE_ME_region", // e.g. us-east-1

  // --- Per-run limits (section 3.3). TODO(confirm) Q4: real values from cost owner. ---
  LIMITS: {
    DOC_MAX_FILES: 10,
    DOC_MAX_FILE_MB: 50,
    DOC_MAX_TOTAL_MB: 150,
    TICKET_MAX_FILE_MB: 50,
    DOC_ACCEPT: [".pdf", ".docx", ".txt", ".md", ".html"],
    TICKET_ACCEPT: [".csv", ".json", ".xlsx"],
  },

  // How long a single step may run before the "still working" callout appears.
  // TODO(confirm) Q6: 60s is too low given observed Bedrock Data Automation times
  // (normalize_docs ran ~48-77s on real runs); 120s recommended. See OPEN_QUESTIONS.md.
  STALE_AFTER: 120,

  // Mock mode. MUST NOT be true when a real API_BASE_URL is configured.
  // Overridden to true by the ?mock=1 query parameter.
  MOCK: false,
};
