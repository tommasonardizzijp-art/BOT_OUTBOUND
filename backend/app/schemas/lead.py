from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List, Any


class LeadResponse(BaseModel):
    ig_user_id: int
    username: Optional[str] = None
    full_name: Optional[str] = None
    biography: Optional[str] = None
    # Enriched from followers table (best available value across all campaigns)
    follower_count: Optional[int] = None
    following_count: Optional[int] = None
    is_verified: bool = False
    external_url: Optional[str] = None
    profile_pic_url: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    whatsapp: Optional[str] = None
    bio_links: List[Any] = []
    scraping_accounts: List[str] = []   # usernames degli account che hanno scrapato il lead
    # Contact info
    contact_history: List[Any] = []  # [{campaign_id, campaign_name, account_username, contacted_at}]
    contacts_count: int = 0          # number of times contacted
    scrape_sources: List[str] = []   # target_username of campaigns that scraped this lead
    has_replied: bool = False
    first_seen_at: Optional[datetime] = None
    last_contacted_at: Optional[datetime] = None
    created_at: datetime


class LeadInsights(BaseModel):
    scraped_leads: int          # unique users scraped (Follower table, lista — solo info base)
    total_leads: int            # lead con info acquisite (righe GlobalContact = bio/contatti estratti)
    contacted_leads: int        # lead davvero contattati via DM (GlobalContact.last_contacted_at not null)
    total_replied: int
    reply_rate: float           # percentage (0-100) sui contattati


class LeadListResponse(BaseModel):
    items: List[LeadResponse]
    total: int
    page: int
    page_size: int
    insights: LeadInsights
