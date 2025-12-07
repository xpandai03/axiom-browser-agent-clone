import random
from typing import List, Dict
from datetime import datetime

def execute_workflow(steps: List[Dict]) -> List[Dict]:
    logs = []
    
    for i, step in enumerate(steps):
        action = step.get("action", "unknown")
        timestamp = datetime.now().isoformat()
        
        if action == "error":
            logs.append({
                "step": i + 1,
                "action": action,
                "status": "failed",
                "message": step.get("message", "Unknown error"),
                "timestamp": timestamp
            })
            continue
        
        log_entry = {
            "step": i + 1,
            "action": action,
            "status": "success",
            "timestamp": timestamp
        }
        
        if action == "goto":
            url = step.get("url", "unknown")
            log_entry["details"] = f"Navigated to {url}"
        elif action == "click":
            selector = step.get("selector", "unknown")
            log_entry["details"] = f"Clicked element: {selector}"
        elif action == "type":
            selector = step.get("selector", "unknown")
            value = step.get("value", "")
            display_value = value if len(value) < 20 else value[:17] + "..."
            log_entry["details"] = f"Typed '{display_value}' into {selector}"
        elif action == "upload":
            selector = step.get("selector", "unknown")
            file = step.get("file", "unknown")
            log_entry["details"] = f"Uploaded {file} to {selector}"
        elif action == "wait":
            duration = step.get("duration", 1000)
            log_entry["details"] = f"Waited {duration}ms"
        elif action == "scroll":
            log_entry["details"] = "Scrolled page"
        else:
            log_entry["details"] = f"Executed {action}"
        
        logs.append(log_entry)
    
    return logs
