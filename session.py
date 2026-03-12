import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from dataclasses import dataclass, field
import logging

from config import SESSION_TIMEOUT

logger = logging.getLogger(__name__)

@dataclass
class UserSession:
    """User session data for tracking ongoing processes"""
    user_id: int
    job_id: str
    json_data: Dict[str, Any]
    json_file_path: Optional[str] = None
    title: Optional[str] = None
    episode: Optional[str] = None
    has_subtitle: bool = False
    subtitle_yes_no: Optional[str] = None
    status: str = "awaiting_info"
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    progress_message_id: Optional[int] = None
    
    def is_expired(self) -> bool:
        """Check if session has expired"""
        return datetime.now() - self.last_activity > timedelta(seconds=SESSION_TIMEOUT)
    
    def update_activity(self):
        """Update last activity timestamp"""
        self.last_activity = datetime.now()


class SessionManager:
    """Manages user sessions"""
    
    def __init__(self):
        self.sessions: Dict[int, UserSession] = {}
    
    def create_session(self, user_id: int, json_data: Dict[str, Any], json_file_path: str = None) -> UserSession:
        """Create new session for user"""
        self.cleanup_expired()
        
        # Hapus session lama jika ada
        if user_id in self.sessions:
            logger.info(f"Removing existing session for user {user_id}")
            del self.sessions[user_id]
        
        session = UserSession(
            user_id=user_id,
            job_id=str(uuid.uuid4()),
            json_data=json_data,
            json_file_path=json_file_path
        )
        self.sessions[user_id] = session
        logger.info(f"Created new session for user {user_id} with job_id {session.job_id}")
        return session
    
    def get_session(self, user_id: int) -> Optional[UserSession]:
        """Get user session if exists and not expired"""
        session = self.sessions.get(user_id)
        if session:
            if session.is_expired():
                logger.info(f"Session expired for user {user_id}")
                del self.sessions[user_id]
                return None
            session.update_activity()
        return session
    
    def delete_session(self, user_id: int):
        """Delete user session"""
        if user_id in self.sessions:
            logger.info(f"Deleted session for user {user_id}")
            del self.sessions[user_id]
    
    def force_cleanup_session(self, user_id: int) -> bool:
        """
        Force cleanup session tanpa cek apapun
        Returns True jika session ada dan dihapus
        """
        if user_id in self.sessions:
            logger.info(f"Force cleanup session for user {user_id}")
            del self.sessions[user_id]
            return True
        return False
    
    def has_active_session(self, user_id: int) -> bool:
        """Check if user has active session"""
        session = self.get_session(user_id)
        return session is not None
    
    def cleanup_expired(self):
        """Remove all expired sessions"""
        expired = [uid for uid, session in self.sessions.items() if session.is_expired()]
        for uid in expired:
            logger.info(f"Cleaning up expired session for user {uid}")
            del self.sessions[uid]
        if expired:
            logger.info(f"Cleaned up {len(expired)} expired sessions")
    
    def update_session_status(self, user_id: int, status: str):
        """Update session status"""
        session = self.get_session(user_id)
        if session:
            session.status = status
            session.update_activity()
    
    def update_session_info(self, user_id: int, title: str = None, episode: str = None, 
                           subtitle_yes_no: str = None):
        """Update session with user provided info"""
        session = self.get_session(user_id)
        if session:
            if title is not None:
                session.title = title
                logger.info(f"Updated title for user {user_id}: {title}")
            if episode is not None:
                session.episode = episode
                logger.info(f"Updated episode for user {user_id}: {episode}")
            if subtitle_yes_no is not None:
                session.subtitle_yes_no = subtitle_yes_no
                session.has_subtitle = subtitle_yes_no.lower() in ['ya', 'yes', 'y', 'true']
                logger.info(f"Updated subtitle for user {user_id}: {subtitle_yes_no}")
            session.update_activity()
    
    def set_progress_message(self, user_id: int, message_id: int):
        """Set progress message ID for updates"""
        session = self.get_session(user_id)
        if session:
            session.progress_message_id = message_id
            logger.info(f"Set progress message for user {user_id}: {message_id}")
    
    def update_session_direct(self, user_id: int, title: str, episode: str, has_subtitle: bool):
        """Update session with extracted data directly"""
        session = self.get_session(user_id)
        if session:
            session.title = title
            session.episode = episode
            session.has_subtitle = has_subtitle
            session.subtitle_yes_no = "Ya" if has_subtitle else "Tidak"
            session.update_activity()
            logger.info(f"Updated session for user {user_id}: {title} - Ep {episode}")
    
    def get_session_count(self) -> int:
        """Get number of active sessions"""
        self.cleanup_expired()
        return len(self.sessions)
    
    def get_all_sessions(self) -> Dict[int, UserSession]:
        """Get all active sessions"""
        self.cleanup_expired()
        return self.sessions.copy()