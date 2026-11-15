"""
Error Handler - טיפול חכם בשגיאות עם retry logic
"""

import time
import functools
from typing import Callable, Any, Optional
import logging

# הגדר logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
for _noisy in ('httpx', 'httpcore'):                  # at INFO they log each URL (URLs can carry keys)
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def retry_on_failure(max_attempts: int = 3, delay: int = 2, backoff: int = 2):
    """
    Decorator לניסיון חוזר במקרה של שגיאה
    
    Args:
        max_attempts: מספר ניסיונות מקסימלי
        delay: המתנה בשניות בין ניסיונות
        backoff: מכפיל להמתנה (exponential backoff)
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            current_delay = delay
            
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                
                except Exception as e:
                    if attempt == max_attempts:
                        logger.error(f"❌ {func.__name__} נכשל אחרי {max_attempts} ניסיונות: {e}")
                        raise
                    
                    logger.warning(f"⚠️  {func.__name__} נכשל (ניסיון {attempt}/{max_attempts}): {e}")
                    logger.info(f"⏳ ממתין {current_delay} שניות לפני ניסיון נוסף...")
                    
                    time.sleep(current_delay)
                    current_delay *= backoff
            
        return wrapper
    return decorator


class SafeExecutor:
    """מריץ פונקציות בצורה בטוחה עם logging"""
    
    @staticmethod
    def execute(
        func: Callable,
        *args,
        task_name: str = "Task",
        critical: bool = False,
        **kwargs
    ) -> Optional[Any]:
        """
        מריץ פונקציה בצורה בטוחה
        
        Args:
            func: הפונקציה להרצה
            task_name: שם המשימה ל-logging
            critical: האם זו משימה קריטית (לזרוק exception)
        
        Returns:
            תוצאת הפונקציה או None במקרה של שגיאה
        """
        try:
            logger.info(f"▶️  מתחיל: {task_name}")
            result = func(*args, **kwargs)
            logger.info(f"✅ הושלם: {task_name}")
            return result
        
        except Exception as e:
            logger.error(f"❌ נכשל: {task_name} - {e}")
            
            if critical:
                raise
            
            return None


# דוגמאות שימוש:

@retry_on_failure(max_attempts=3, delay=2)
def download_with_retry(url: str):
    """דוגמה - הורדה עם retry"""
    # קוד ההורדה כאן
    pass


# או בלי decorator:
def some_risky_function():
    executor = SafeExecutor()
    
    result = executor.execute(
        download_file,
        url="http://example.com/file.pdf",
        task_name="הורדת קובץ PDF",
        critical=False  # לא לעצור אם נכשל
    )
    
    if result:
        print("הצלחה!")
    else:
        print("נכשל, ממשיכים...")
