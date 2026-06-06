// Account types
export type AccountStatus = 'active' | 'warming_up' | 'cooldown' | 'banned' | 'challenge_required' | 'disabled'

export interface Account {
  id: string
  username: string
  proxy: string | null
  status: AccountStatus
  daily_message_count: number
  daily_message_limit: number
  total_messages_sent: number
  warmup_day: number
  cooldown_until: string | null
  last_activity_at: string | null
  last_login_at: string | null
  notes: string | null
  created_at: string
  updated_at: string
}

export interface AccountCreate {
  username: string
  password: string
  proxy?: string
  daily_message_limit?: number
  notes?: string
}

// Campaign types
export type CampaignStatus = 'draft' | 'scraping' | 'scraping_break' | 'scraping_and_running' | 'ready' | 'running' | 'paused' | 'completed' | 'error'

export interface Campaign {
  id: string
  name: string
  target_username: string | null
  // 'scrape' = scrape follower/following pagina; 'import' = lista profili da file
  source_type: 'scrape' | 'import'
  target_user_id: number | null
  base_message_template: string | null
  ai_prompt_context: string | null
  // Solo raccolta lead quando false — nessun DM viene inviato
  messaging_enabled: boolean
  // Cap lookup/giorno per account durante lo scraping (anti-ban); null = nessun cap
  scrape_daily_limit: number | null
  // M10: A/B testing — second template (optional)
  message_template_b: string | null
  status: CampaignStatus
  total_followers: number
  messages_sent: number
  messages_failed: number
  messages_pending: number
  messages_skipped: number
  messages_replied: number
  reply_rate: number  // 0.0 — 1.0
  daily_limit: number | null
  messages_sent_today: number  // DMs sent today (computed by backend)
  // M15 rev: per-campaign approval sampling
  require_approval: boolean
  approval_sample_size: number
  // 'followers' = scrape who follows target; 'following' = scrape who target follows
  scrape_mode: 'followers' | 'following'
  scrape_completed_at: string | null
  started_at: string | null
  completed_at: string | null
  created_at: string
  updated_at: string
  // Session break config (per-campaign)
  scrape_session_size: number
  scrape_break_minutes_min: number
  scrape_break_minutes_max: number
  bio_fetch_delay_min: number
  bio_fetch_delay_max: number
  auto_generate: boolean
  scrape_break_until: string | null
  scrape_cursor: string | null
  scrape_outcome: string | null
}

// M15 rev: approval queue item
export interface ApprovalQueueItem {
  follower_id: string
  username: string
  full_name: string | null
  biography: string | null
  follower_count: number | null
  is_verified: boolean
  message_id: string | null
  generated_text: string | null
  template_variant: string | null
}

export interface ApprovalQueue {
  items: ApprovalQueueItem[]
  total: number
}

export interface CampaignCreate {
  name: string
  target_username?: string | null
  source_type?: 'scrape' | 'import'
  base_message_template?: string | null
  ai_prompt_context?: string
  message_template_b?: string | null
  messaging_enabled?: boolean
  scrape_daily_limit?: number | null
  daily_limit?: number | null
  require_approval?: boolean
  approval_sample_size?: number
  scrape_mode?: 'followers' | 'following'
  scrape_session_size?: number
  scrape_break_minutes_min?: number
  scrape_break_minutes_max?: number
  bio_fetch_delay_min?: number
  bio_fetch_delay_max?: number
}

// M10: A/B testing stats
export interface ABVariantStats {
  total: number
  sent: number
  failed: number
  pending: number
  replied: number
  reply_rate: number  // 0.0 — 1.0
}

export interface ABStats {
  template_b_present: boolean
  variant_a: ABVariantStats | null
  variant_b: ABVariantStats | null
}

// Campaign ↔ Account assignment types
export type AccountRole = 'scraping' | 'dm' | 'both'

export interface CampaignAccount {
  id: string
  campaign_id: string
  account_id: string
  account_username: string
  daily_limit_override: number | null
  is_active: boolean
  role: AccountRole
  created_at: string
}

export interface CampaignAccountAssign {
  account_id: string
  daily_limit_override?: number | null
  role?: AccountRole
}

export interface CampaignAccountUpdate {
  daily_limit_override?: number | null
  is_active?: boolean
  role?: AccountRole
}

// Follower types
export type FollowerStatus = 'pending' | 'bio_scraped' | 'message_generated' | 'pending_approval' | 'sent' | 'failed' | 'skipped' | 'replied'

export interface Follower {
  id: string
  campaign_id: string
  ig_user_id: number
  username: string
  full_name: string | null
  biography: string | null
  is_private: boolean
  is_verified: boolean
  follower_count: number | null
  following_count: number | null
  profile_pic_url: string | null
  external_url: string | null
  phone: string | null
  email: string | null
  whatsapp: string | null
  bio_links: string | null
  status: FollowerStatus
  skip_reason: string | null
  generated_text: string | null
  template_variant: string | null
  created_at: string
  updated_at: string
}

export interface FollowerListResponse {
  items: Follower[]
  total: number
  page: number
  page_size: number
}

// Message types
export type MessageStatus = 'pending' | 'sent' | 'failed' | 'retry' | 'sending'

export interface Message {
  id: string
  campaign_id: string
  campaign_name: string | null
  follower_id: string
  follower_username: string | null
  follower_full_name: string | null
  account_id: string | null
  account_username: string | null
  generated_text: string
  status: MessageStatus
  has_reply: boolean
  error_message: string | null
  retry_count: number
  // M10: 'a' | 'b' | null (null for messages before M10)
  template_variant: string | null
  sent_at: string | null
  created_at: string
  updated_at: string
}

export interface MessageListResponse {
  items: Message[]
  total: number
  page: number
  page_size: number
}

// Dashboard types
export interface DashboardStats {
  total_accounts: number
  active_accounts: number
  accounts_in_cooldown: number
  accounts_banned: number
  total_campaigns: number
  running_campaigns: number
  messages_sent_today: number
  messages_sent_total: number
  messages_failed_total: number
  success_rate: number
}

export interface ActivityLog {
  id: string
  account_id: string | null
  campaign_id: string | null
  action: string
  details: string | null
  created_at: string
}

export interface ActivityLogListResponse {
  items: ActivityLog[]
  total: number
}

// Timeline
export interface HourlyPoint {
  hour: string
  count: number
}

export interface TimelineResponse {
  data: HourlyPoint[]
}

// M8 lite: DM inbox count
export interface DMCount {
  unread_count: number
  pending_count: number
  checked_at: string
}

// M9: Account metrics
export interface AccountMetrics {
  today_sent: number
  today_limit: number
  total_sent: number
  total_failed: number
  success_rate: number
  ban_events: number
  challenge_events: number
  warmup_day: number
  daily_message_count: number
}

// M7: Leads
export interface ContactHistoryEntry {
  campaign_id: string
  campaign_name: string | null
  account_id: string | null
  account_username: string | null
  contacted_at: string
}

export interface Lead {
  ig_user_id: number
  username: string | null
  full_name: string | null
  biography: string | null
  follower_count: number | null
  following_count: number | null
  is_verified: boolean
  external_url: string | null
  profile_pic_url: string | null
  phone: string | null
  email: string | null
  whatsapp: string | null
  bio_links: { url: string; title: string | null }[]
  scraping_accounts: string[]
  contact_history: ContactHistoryEntry[]
  contacts_count: number
  scrape_sources: string[]
  has_replied: boolean
  last_contacted_at: string | null
  created_at: string
}

export interface LeadInsights {
  scraped_leads: number
  total_leads: number
  total_replied: number
  reply_rate: number
}

export interface MessageStats {
  total_sent: number
  total_failed: number
  total_replied: number
  success_rate: number
  reply_rate: number
}

export interface LeadListResponse {
  items: Lead[]
  total: number
  page: number
  page_size: number
  insights: LeadInsights
}

// Worker events (real-time log feed)
export interface WorkerEvent {
  id: number
  ts: string
  campaign_id: string
  action: string
  detail: string
  level: 'info' | 'warn' | 'error'
}

export interface WorkerEventsResponse {
  events: WorkerEvent[]
  last_id: number
}

// Health
export interface HealthStatus {
  status: string
  ollama: string
  redis: string
  database: string
}

// Import profiles (lista da file)
export interface ImportStatusResponse {
  total: number
  pending: number
  resolved: number
  not_found: number
  private: number
  error: number
}
export interface ImportUploadResponse {
  inserted: number
  duplicates_in_file: number
  skipped_existing: number
  skipped_invalid: number
}

export interface BotState {
  halted: boolean
  halted_reason: string | null
  halted_kind: string | null
  halted_at: string | null
  halted_by: string | null
  last_resume_at: string | null
  last_resume_by: string | null
}
