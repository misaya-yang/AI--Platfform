/**
 * CLI configuration types.
 * Config stored at ~/.ai-gateway/config.json
 */

export interface CLIConfig {
  api_key: string;
  base_url: string;
  tenant_id: string;
  user_id?: string;
  default_model: string;
  kb_dataset_ids?: string[];
  timeout: number;
  max_retries: number;
}

export interface PermissionsConfig {
  allowed_dirs: string[];
  blocked_patterns: string[];
  auto_approve: string[];
  require_confirm: string[];
}

export interface MCPServerConfig {
  name: string;
  command: string;
  args: string[];
  env?: Record<string, string>;
}

export const DEFAULT_CONFIG: CLIConfig = {
  api_key: "",
  base_url: "http://localhost:8080",
  tenant_id: "",
  default_model: "qwen3.6-plus",
  timeout: 30,
  max_retries: 3,
};

export const DEFAULT_PERMISSIONS: PermissionsConfig = {
  allowed_dirs: [],
  blocked_patterns: ["*.env", "*.key", "*.pem", "*.p12", ".git/objects/**"],
  auto_approve: ["read_file", "glob", "grep", "list_dir", "tree"],
  require_confirm: ["write_file", "edit_file", "bash"],
};
