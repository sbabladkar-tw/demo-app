from flask import Flask, request, jsonify
import requests
import os
import logging
from datetime import datetime, UTC
from github import Github
import yaml
import json
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)

# === Configuration ===
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")  # e.g., "username/repo"
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
TARGET_MANIFEST_PATH = "app/failing-app.yaml"

# === Logging Setup ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# ... [All your definition and utility functions remain unchanged, such as format_alert, pr_already_exists, etc.] ...
def format_alert(alert):
    """Format alert data for better readability"""
    status = alert.get("status", "unknown").upper()
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    starts_at = alert.get("startsAt", "N/A")
    ends_at = alert.get("endsAt", "N/A")

    return f"""
🚨 *Status:* `{status}`
📛 *Alert:* `{labels.get('alertname', 'unknown')}`
📦 *Instance:* `{labels.get('instance', '-')}`
☁️ *Cluster:* `{labels.get('cluster', '-')}`
📦 *Pod:* `{labels.get('pod', '-')}`
📂 *Container:* `{labels.get('container', '-')}`
📝 *Description:* `{annotations.get('description', '-')}`
💬 *Summary:* `{annotations.get('summary', '-')}`
🕐 *Starts At:* `{starts_at}`
🕐 *Ends At:* `{ends_at}`
"""

def pr_already_exists(repo, branch_prefix):
    """Check if a similar PR already exists"""
    pulls = repo.get_pulls(state='open', base=GITHUB_BRANCH)
    for pr in pulls:
        if pr.head.ref.startswith(branch_prefix):
            return pr.html_url
    return None

def get_current_manifest():
    """Fetch current manifest from GitHub"""
    try:
        g = Github(GITHUB_TOKEN)
        repo = g.get_repo(GITHUB_REPO)
        contents = repo.get_contents(TARGET_MANIFEST_PATH, ref=GITHUB_BRANCH)
        return contents.decoded_content.decode("utf-8")
    except Exception as e:
        log.error(f"❌ Failed to fetch current manifest: {e}")
        return None

def validate_image_exists(image_name):
    """Enhanced image validation with registry check and better suggestions"""
    try:
        log.info(f"🔍 Validating image: {image_name}")
        
        # Basic format validation
        if not image_name or ":" not in image_name:
            return False, "Image format invalid - should contain tag (image:tag)"
        
        parts = image_name.split(":")
        if len(parts) != 2:
            return False, "Image format invalid - should be image:tag format"
        
        image_repo, tag = parts
        if not image_repo or not tag:
            return False, "Image repository or tag is empty"
        
        # Check for common typos or invalid characters
        if any(char in image_name for char in [" ", "\t", "\n"]):
            return False, "Image name contains whitespace characters"
        
        # Try to verify image exists by checking Docker Hub API (optional)
        try:
            if "/" not in image_repo:  # Official images like nginx, python
                registry_url = f"https://registry.hub.docker.com/v2/library/{image_repo}/tags/list"
            else:  # User/org images
                registry_url = f"https://registry.hub.docker.com/v2/{image_repo}/tags/list"
            
            import requests
            response = requests.get(registry_url, timeout=10)
            
            if response.status_code == 200:
                tags_data = response.json()
                available_tags = [t["name"] for t in tags_data.get("results", [])]
                if tag in available_tags:
                    return True, f"Image verified in Docker Hub with tag {tag}"
                else:
                    # Suggest similar tags
                    suggested_tags = [t for t in available_tags if tag.lower() in t.lower() or t in ["latest", "alpine", "slim"]][:3]
                    suggestion = f", try: {', '.join(suggested_tags)}" if suggested_tags else ""
                    return False, f"Tag '{tag}' not found for {image_repo}{suggestion}"
            else:
                # If registry check fails, allow it but warn
                log.warning(f"Could not verify image in registry (status: {response.status_code})")
                return True, f"Image format valid, registry check inconclusive"
                
        except Exception as registry_error:
            log.warning(f"Registry check failed: {registry_error}")
            # If registry check fails, do basic validation
            return True, "Image format appears valid (registry check failed)"
        
    except Exception as e:
        return False, f"Image validation error: {str(e)}"

def clean_json_string(json_str):
    """Clean and fix common JSON formatting issues from LLM responses"""
    import re
    
    # Remove any leading/trailing whitespace
    json_str = json_str.strip()
    
    # Fix single quotes to double quotes (but be careful with apostrophes in strings)
    # This is a simple approach - for more complex cases, use a proper JSON5 parser
    json_str = re.sub(r"'([^']*)':", r'"\1":', json_str)  # Fix single quoted keys
    json_str = re.sub(r":\s*'([^']*)'", r': "\1"', json_str)  # Fix single quoted values
    
    # Fix common boolean/null issues
    json_str = re.sub(r'\bTrue\b', 'true', json_str)
    json_str = re.sub(r'\bFalse\b', 'false', json_str)
    json_str = re.sub(r'\bNone\b', 'null', json_str)
    
    # Fix trailing commas
    json_str = re.sub(r',(\s*[}\]])', r'\1', json_str)
    
    return json_str

def parse_ollama_instructions(ollama_response):
    """Enhanced parsing with better error handling and image-specific validation"""
    try:
        log.info("🔍 Parsing Ollama response for JSON instructions...")
        
        # Multiple strategies to find JSON
        json_str = None
        
        # Strategy 1: Look for ```json blocks
        json_pattern = r'```json\s*\n(.*?)\n```'
        import re
        json_matches = re.findall(json_pattern, ollama_response, re.DOTALL)
        if json_matches:
            json_str = json_matches[0].strip()
            log.info("Found JSON in ```json block")
        
        # Strategy 2: Look for first { to last } 
        if not json_str:
            start_idx = ollama_response.find('{')
            end_idx = ollama_response.rfind('}')
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                json_str = ollama_response[start_idx:end_idx+1]
                log.info("Found JSON by brace matching")
        
        if not json_str:
            log.error("❌ No JSON structure found in Ollama response")
            log.debug(f"Response preview: {ollama_response[:500]}")
            return extract_fallback_instructions(ollama_response)
        
        # Clean the JSON
        json_str = clean_json_string(json_str)
        log.info(f"📝 Cleaned JSON: {json_str[:200]}...")
        
        # Parse JSON
        instructions = json.loads(json_str)
        
        # Validate and fix image changes
        if instructions and 'changes' in instructions:
            for change in instructions['changes']:
                if change.get('type') == 'update_image':
                    image_value = change.get('value')
                    if isinstance(image_value, dict):
                        # Fix common mistake where value is object instead of string
                        log.warning("⚠️ Found image change with object value, attempting to fix...")
                        if 'image' in image_value:
                            change['value'] = image_value['image']
                        elif 'name' in image_value:
                            change['value'] = image_value['name'] 
                        else:
                            # Try to construct image string
                            repo = image_value.get('repository', image_value.get('repo', 'nginx'))
                            tag = image_value.get('tag', 'latest')
                            change['value'] = f"{repo}:{tag}"
                        log.info(f"🔧 Fixed image value to: {change['value']}")
                    
                    # Validate the final image name
                    if isinstance(change['value'], str) and ':' in change['value']:
                        log.info(f"✅ Valid image format: {change['value']}")
                    else:
                        log.warning(f"⚠️ Invalid image format: {change['value']}, using fallback")
                        change['value'] = 'nginx:latest'  # Safe fallback
        
        log.info("✅ Instructions parsed and validated successfully")
        return instructions
        
    except json.JSONDecodeError as e:
        log.error(f"❌ JSON parsing failed: {e}")
        log.error(f"JSON string was: {json_str[:300] if json_str else 'None'}...")
        return extract_fallback_instructions(ollama_response)
    except Exception as e:
        log.error(f"❌ Unexpected error parsing instructions: {e}")
        return extract_fallback_instructions(ollama_response)

def validate_instructions_structure(instructions):
    """Validate that instructions have required structure"""
    if not isinstance(instructions, dict):
        return False
    
    required_fields = ['problem_type', 'requires_pr']
    return all(field in instructions for field in required_fields)

def extract_fallback_instructions(ollama_response):
    """Enhanced fallback with better image change detection"""
    try:
        log.info("🔧 Using enhanced fallback instruction extraction...")
        
        response_lower = ollama_response.lower()
        
        instructions = {
            "problem_type": "other",
            "problem_analysis": "Fallback analysis - JSON parsing failed",
            "validation_required": False,
            "changes": [],
            "expected_impact": "Manual review required",
            "requires_pr": False
        }
        
        # Enhanced problem type detection
        problem_patterns = {
            "image_pull_backoff": [
                "imagepullbackoff", "image pull", "pull backoff", "errimagepull",
                "image not found", "pull error", "repository does not exist",
                "manifest unknown", "unauthorized", "image pull failed"
            ],
            "readiness_probe_failure": [
                "readiness probe", "readiness check", "ready probe", "/health",
                "health check", "probe failed", "readiness"
            ],
            "liveness_probe_failure": [
                "liveness probe", "liveness check", "live probe", "liveness"
            ],
            "command_failure": [
                "command failed", "command error", "exec error", "entrypoint",
                "exit code", "crashed", "container exited", "command not found"
            ]
        }
        
        detected_type = "other"
        for problem_type, keywords in problem_patterns.items():
            if any(keyword in response_lower for keyword in keywords):
                detected_type = problem_type
                log.info(f"🎯 Detected problem type: {problem_type}")
                break
        
        instructions["problem_type"] = detected_type
        
        # Create specific fixes based on detected type
        if detected_type == "image_pull_backoff":
            # Try to extract mentioned images and suggest replacements
            suggested_image = extract_suggested_image_from_text(ollama_response)
            instructions["changes"] = [{
                "type": "update_image",
                "path": ["0"],
                "value": suggested_image,
                "description": f"Replace failing image with working {suggested_image}"
            }]
            instructions["requires_pr"] = True
            instructions["validation_required"] = True
            instructions["validation_type"] = "image_check"
            instructions["validation_data"] = {"image": suggested_image}
            instructions["expected_impact"] = "Pod should pull image successfully"
            
        elif detected_type == "command_failure":
            suggested_command = extract_command_from_text(ollama_response)
            instructions["changes"] = [{
                "type": "update_command",
                "path": ["0"],
                "value": suggested_command,
                "description": "Fix container command to prevent exit"
            }]
            instructions["requires_pr"] = True
            instructions["expected_impact"] = "Container should start and stay running"
            
        # Add other cases as needed...
        
        return instructions
        
    except Exception as e:
        log.error(f"❌ Enhanced fallback extraction failed: {e}")
        return None

def extract_suggested_image_from_text(text):
    """Extract or suggest a working image from the response text"""
    text_lower = text.lower()
    
    # Look for specific application mentions and suggest appropriate images
    if any(word in text_lower for word in ['nginx', 'web server']):
        return 'nginx:1.21'
    elif any(word in text_lower for word in ['apache', 'httpd']):
        return 'httpd:2.4'
    elif any(word in text_lower for word in ['python', 'flask', 'django']):
        return 'python:3.9-slim'
    elif any(word in text_lower for word in ['node', 'nodejs', 'npm']):
        return 'node:16-alpine'
    elif any(word in text_lower for word in ['java', 'openjdk']):
        return 'openjdk:11-jre-slim'
    else:
        return 'nginx:latest'  # Safe default

def extract_command_from_text(text):
    """Try to extract command suggestions from text when JSON parsing fails"""
    try:
        # Look for common command patterns in the text
        text_lower = text.lower()
        
        # Default safe command
        default_command = ["/bin/sh", "-c", "echo 'Command extraction failed - manual review needed'"]
        
        # Look for specific command suggestions
        if "sleep" in text_lower:
            return ["/bin/sh", "-c", "sleep infinity"]
        elif "nginx" in text_lower:
            return ["nginx", "-g", "daemon off;"]
        elif "apache" in text_lower or "httpd" in text_lower:
            return ["httpd-foreground"]
        elif "python" in text_lower:
            return ["python", "-c", "import time; time.sleep(3600)"]
        elif "node" in text_lower:
            return ["node", "-e", "setInterval(() => console.log('alive'), 10000)"]
        
        return default_command
        
    except Exception as e:
        log.error(f"❌ Command extraction failed: {e}")
        return ["/bin/sh", "-c", "echo 'Command extraction error'"]

def find_containers_in_manifest(manifest):
    """Find containers in various Kubernetes resource types"""
    kind = manifest.get('kind', '').lower()
    
    # Different paths for different Kubernetes resources
    container_paths = {
        'deployment': ['spec', 'template', 'spec', 'containers'],
        'daemonset': ['spec', 'template', 'spec', 'containers'], 
        'statefulset': ['spec', 'template', 'spec', 'containers'],
        'replicaset': ['spec', 'template', 'spec', 'containers'],
        'pod': ['spec', 'containers'],
        'job': ['spec', 'template', 'spec', 'containers'],
        'cronjob': ['spec', 'jobTemplate', 'spec', 'template', 'spec', 'containers']
    }
    
    # Try the specific path for the resource kind
    if kind in container_paths:
        path = container_paths[kind]
        containers = manifest
        for key in path:
            containers = containers.get(key, {})
            if not containers:
                break
        if isinstance(containers, list):
            log.info(f"✅ Found containers using {kind} path: {len(containers)} containers")
            return containers, path
    
    # Fallback: search common paths
    fallback_paths = [
        ['spec', 'template', 'spec', 'containers'],  # Most common
        ['spec', 'containers'],  # Pod
        ['spec', 'jobTemplate', 'spec', 'template', 'spec', 'containers']  # CronJob
    ]
    
    for path in fallback_paths:
        containers = manifest
        for key in path:
            containers = containers.get(key, {})
            if not containers:
                break
        if isinstance(containers, list) and containers:
            log.info(f"✅ Found containers using fallback path {path}: {len(containers)} containers")
            return containers, path
    
    log.error("❌ Could not find containers in manifest")
    return None, None

def apply_manifest_changes(yaml_content, changes, target_manifest_name=None):
    """Apply changes to Kubernetes manifests in a multi-document YAML file with enhanced debugging"""
    try:
        manifests = list(yaml.safe_load_all(yaml_content))
        
        if not manifests:
            raise ValueError("No manifests found in YAML content")

        # If a target manifest name is provided, find that manifest for modification
        if target_manifest_name:
            manifest = None
            for m in manifests:
                if m.get("metadata", {}).get("name") == target_manifest_name:
                    manifest = m
                    break
            if manifest is None:
                raise ValueError(f"Manifest with name '{target_manifest_name}' not found")
        else:
            # Default to modifying the first manifest if no name provided
            manifest = manifests[0]
        
        log.info(f"📄 Manifest kind: {manifest.get('kind', 'Unknown')} | name: {manifest.get('metadata', {}).get('name', 'unknown')}")
        log.info(f"🔧 Applying {len(changes)} changes to manifest")
        
        containers, container_path = find_containers_in_manifest(manifest)
        
        if not containers:
            raise ValueError("No containers found in manifest - unsupported resource type or structure")
        
        log.info(f"📦 Found {len(containers)} containers at path: {' -> '.join(container_path)}")
        
        for i, change in enumerate(changes):
            change_type = change.get('type')
            path = change.get('path', ['0'])  # Default to first container
            value = change.get('value')
            description = change.get('description', 'No description')
            
            # Enhanced debugging logging
            log.info(f"🔧 Change {i+1}: {change_type}")
            log.info(f"   Path: {path}")
            log.info(f"   Value: {value}")
            log.info(f"   Value type: {type(value)}")
            log.info(f"   Description: {description}")

            try:
                container_idx = int(path[0]) if path and path[0].isdigit() else 0
            except (ValueError, IndexError):
                container_idx = 0
                
            if container_idx >= len(containers):
                log.warning(f"⚠️ Container index {container_idx} out of range (only {len(containers)} containers)")
                container_idx = 0
            
            container = containers[container_idx]
            log.info(f"📦 Modifying container {container_idx}: {container.get('name', 'unnamed')}")

            # Apply the specific change with enhanced logging
            if change_type == 'update_image':
                old_image = container.get('image', 'none')
                
                # Ensure value is a string - this is the key fix for image issues
                if isinstance(value, dict):
                    log.error(f"❌ Image value is dict, not string: {value}")
                    # Try to extract string from dict
                    if 'image' in value:
                        value = value['image']
                    elif 'name' in value:
                        value = value['name']
                    elif 'repository' in value and 'tag' in value:
                        value = f"{value['repository']}:{value['tag']}"
                    elif 'repo' in value and 'tag' in value:
                        value = f"{value['repo']}:{value['tag']}"
                    else:
                        value = 'nginx:latest'  # Fallback
                    log.info(f"🔧 Converted image value to string: {value}")
                
                # Final validation that we have a proper image string
                if not isinstance(value, str) or ':' not in value:
                    log.warning(f"⚠️ Invalid image format after conversion: {value}, using fallback")
                    value = 'nginx:latest'
                
                container['image'] = value
                log.info(f"🖼️ Image updated successfully:")
                log.info(f"   From: {old_image}")
                log.info(f"   To: {value}")
                
            elif change_type == 'update_command':
                old_command = container.get('command', [])
                # Ensure command is a list
                if isinstance(value, str):
                    container['command'] = [value]
                elif isinstance(value, list):
                    container['command'] = value
                else:
                    log.warning(f"⚠️ Invalid command type: {type(value)}, converting to list")
                    container['command'] = [str(value)]
                    
                log.info(f"⚙️ Command updated:")
                log.info(f"   From: {old_command}")
                log.info(f"   To: {container['command']}")
                
            elif change_type == 'update_args':
                old_args = container.get('args', [])
                # Ensure args is a list
                if isinstance(value, str):
                    container['args'] = [value]
                elif isinstance(value, list):
                    container['args'] = value
                else:
                    log.warning(f"⚠️ Invalid args type: {type(value)}, converting to list")
                    container['args'] = [str(value)]
                    
                log.info(f"📝 Args updated:")
                log.info(f"   From: {old_args}")
                log.info(f"   To: {container['args']}")
                
            elif change_type == 'update_readiness_probe':
                old_probe = container.get('readinessProbe', {})
                if isinstance(value, dict):
                    container['readinessProbe'] = value
                    log.info(f"🔍 Readiness probe updated:")
                    log.info(f"   From: {old_probe}")
                    log.info(f"   To: {value}")
                else:
                    log.error(f"❌ Invalid readiness probe value type: {type(value)}")
                    
            elif change_type == 'update_liveness_probe':
                old_probe = container.get('livenessProbe', {})
                if isinstance(value, dict):
                    container['livenessProbe'] = value
                    log.info(f"💓 Liveness probe updated:")
                    log.info(f"   From: {old_probe}")
                    log.info(f"   To: {value}")
                else:
                    log.error(f"❌ Invalid liveness probe value type: {type(value)}")
                    
            elif change_type == 'add_env_var':
                if 'env' not in container:
                    container['env'] = []
                if isinstance(value, dict) and 'name' in value and 'value' in value:
                    container['env'].append(value)
                    log.info(f"🌍 Environment variable added: {value}")
                else:
                    log.error(f"❌ Invalid env var format: {value}")
                    
            elif change_type == 'update_ports':
                if isinstance(value, list):
                    container['ports'] = value
                    log.info(f"🔌 Ports updated: {value}")
                else:
                    log.error(f"❌ Invalid ports value type: {type(value)}")
                    
            else:
                log.warning(f"⚠️ Unknown change type: {change_type}")

        # Dump all manifests back to multi-doc YAML string
        updated_yaml = yaml.safe_dump_all(manifests, sort_keys=False, default_flow_style=False)
        
        log.info("✅ All changes applied successfully")
        return updated_yaml

    except Exception as e:
        log.error(f"❌ Failed to apply manifest changes: {e}")
        log.error(f"Changes attempted: {json.dumps(changes, indent=2)}")
        raise

def get_manifest_path(problem_type):
    # Map problem types to individual manifest files
    manifest_map = {
        "readiness_probe_failure": "app/readiness-fail.yaml",
        "liveness_probe_failure": "app/liveness-fail.yaml",
        "image_pull_backoff": "app/imagepullbackoff-fail.yaml",
        "command_failure": "app/commandfail-fail.yaml",
        # default fallback manifest file
        "other": "app/failing-app.yaml"
    }
    return manifest_map.get(problem_type, manifest_map["other"])

def create_fix_pr(pod_name, instructions, rca_summary):
    """Create a pull request with the suggested fixes, using separate manifest file per failure type"""
    log.info(f"🔧 Attempting to fix manifest for pod: {pod_name}")
    
    try:
        g = Github(GITHUB_TOKEN)
        repo = g.get_repo(GITHUB_REPO)

        problem_type = instructions.get('problem_type', 'other')
        # Determine the manifest path based on problem type
        manifest_path = get_manifest_path(problem_type)
        print(manifest_path)
        # Fetch the manifest content for the determined path
        contents = repo.get_contents(manifest_path, ref=GITHUB_BRANCH)
        original_content = contents.decoded_content.decode("utf-8")

        # Apply changes based on Ollama instructions
        changes = instructions.get('changes', [])
        if not changes:
            log.warning("⚠️ No changes specified in Ollama instructions")
            return None

        # Modify the manifest content for the specific manifest file
        fixed_content = apply_manifest_changes(original_content, changes)

        # Check if PR already exists for the pod/failure
        branch_prefix = f"fix/{pod_name}"
        existing_pr_url = pr_already_exists(repo, branch_prefix)
        if existing_pr_url:
            log.info("⚠️ Similar PR already exists.")
            return existing_pr_url

        # Create a new branch
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        branch_name = f"{branch_prefix}-{timestamp}"
        base_sha = repo.get_branch(GITHUB_BRANCH).commit.sha
        repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_sha)

        # Create detailed PR description
        change_summary = "\n".join([f"- {change.get('type')}: {change.get('description', 'No description')}" 
                                   for change in changes])
        
        pr_body = f"""## Auto-Generated Fix for Pod: {pod_name}


### Root Cause Analysis Summary:
{instructions.get('problem_analysis', 'Analysis not available')}


### Changes Applied:
{change_summary}


### Impact:
{instructions.get('expected_impact', 'Expected to resolve the pod failure')}


---
*This PR was automatically generated by the K8s Admin AI assistant based on alert analysis.*
"""

        # Update the determined manifest file in the new branch
        repo.update_file(
            contents.path,
            f"Fix: Auto-resolve {instructions.get('problem_type', 'issue')} in {pod_name}",
            fixed_content,
            contents.sha,
            branch=branch_name
        )

        # Create the pull request
        pr = repo.create_pull(
            title=f"🤖 Auto-Fix: Resolve {instructions.get('problem_type', 'issue')} in {pod_name}",
            body=pr_body,
            head=branch_name,
            base=GITHUB_BRANCH
        )

        log.info(f"✅ Pull request created: {pr.html_url}")
        return pr.html_url
        
    except Exception as e:
        log.error(f"❌ Failed to create PR: {e}")
        return None

def compose_slack_alert_blocks(alerts, instructions, rca, pr_url, validation_result):
    """Compose concise Slack blocks with icons, actions, and short timestamp"""
    # Process essential alert fields from the first alert
    main_alert = alerts[0]
    status = main_alert.get("status", "unknown").upper()
    labels = main_alert.get("labels", {})
    annotations = main_alert.get("annotations", {})

    alert_name = labels.get("alertname", "unknown")
    pod_name = labels.get("pod", "unknown")
    cluster = labels.get("cluster", "XConfDemo")
    description = annotations.get("description", "XConf Demo Application")

    # Select emoji by problem type
    problem_emojis = {
        "readiness_probe_failure": "🔍",
        "liveness_probe_failure": "🔍",
        "image_pull_backoff": "🖼️",
        "command_failure": "📝",
        "other": "❓"
    }
    problem_type = (instructions.get("problem_type") if instructions else "other")
    problem_icon = problem_emojis.get(problem_type, "❓")

    # Condense AI analysis to a short sentence
    ai_issue = ""
    if instructions and instructions.get('problem_analysis'):
        ai_issue = instructions.get('problem_analysis', '').split('\n')[0][:120]
    elif rca:
        ai_issue = rca.strip().split('\n')[0][:120]
    else:
        ai_issue = "No analysis available."

    ai_fix = ""
    if instructions and instructions.get('changes'):
        ai_fix = instructions['changes'][0].get('description', '')[:100]

    action_line = ""
    if pr_url:
        action_line = f"🔧 Auto-Fix PR Created - <{pr_url}|Ready to review & merge>"
    elif instructions and instructions.get('requires_pr'):
        action_line = "⚠️ PR creation failed - manual review needed"
    else:
        action_line = "⬆️ Manual investigation required"

    # Shorter timestamp
    short_time = datetime.now(UTC).strftime("%H:%M UTC")

    # Compose Slack message blocks
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🤖 K8s Alert - Auto Analysis",
                "emoji": True
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"🔥 *{alert_name}* ({status})"
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"📦 *Pod:* `{pod_name}` in `{cluster}`\n💬 {description}"
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{problem_icon} *{problem_type.replace('_', ' ').title()}*\n"
                    f"Issue: {ai_issue}\n"
                    f"Fix: {ai_fix}"
                )
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": action_line
            }
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"⏰ {short_time} | :robot_face: *Ollama K8s Admin AI*"
                }
            ]
        },
        { "type": "divider" }
    ]
    # Add validation result if any, as an extra section
    if validation_result:
        blocks.insert(4, {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"🔍 *Validation Result:* ``````"
            }
        })

    return blocks

@app.route("/alerts", methods=["POST"])
def handle_alert():
    """Main webhook endpoint for handling Grafana alerts"""
    log.info("📥 Received alert webhook request")
    data = request.json
    alerts = data.get("alerts", [])

    if not alerts:
        return jsonify({"status": "no alerts received"}), 400

    # Format alerts for analysis
    formatted_alerts = [format_alert(a) for a in alerts]
    alert_summary = "\n---\n".join(formatted_alerts)
    pod_name = alerts[0].get("labels", {}).get("pod", "unknown")
    
    # Get current manifest for context
    current_manifest = get_current_manifest()
    manifest_context = f"\n\nCurrent Manifest:\n``````" if current_manifest else ""

    # Enhanced Ollama prompt for K8s admin intelligence
    ollama_prompt = f"""You are an expert Kubernetes SRE/DevOps administrator. Analyze the following alert and provide intelligent remediation.

ALERT DETAILS:
{alert_summary}
{manifest_context}

CRITICAL: You MUST provide valid JSON with double quotes. No single quotes, no trailing commas, no Python-style True/False/None.

Your task is to:
1. Analyze the EXACT failure reason from the alert
2. Look at the current manifest to understand what's configured
3. Provide a SPECIFIC, WORKING fix (not placeholder text)

## Analysis
[Your detailed root cause analysis here - identify the exact issue type]

## Instructions
Based on your analysis, provide REAL commands, not placeholders.

PROBLEM TYPES (choose the MOST SPECIFIC one):
- readiness_probe_failure: HTTP/TCP readiness probe timing out or returning errors
- liveness_probe_failure: Liveness probe issues causing restarts  
- image_pull_backoff: Cannot pull container image (ImagePullBackOff/ErrImagePull)
- command_failure: Container command/entrypoint failing to start or exiting
- other: Other issues not covered above

CHANGE TYPES (for changes array):
- update_readiness_probe: Fix readiness probe config (path, port, timing)
- update_liveness_probe: Fix liveness probe config
- update_image: Change container image (for ImagePullBackOff)
- update_command: Fix container command array (NOT args)
- update_args: Fix container args array (when command is correct but args are wrong)
- add_env_var: Add required environment variable

IMPORTANT for update_image :
- Never use placeholder text or output like "newRepository": "...", "newTag": "..."
- ALWAYS provide a VALID Docker image string directly as the `"value"` for `"update_image"`, e.g., `"nginx:1.21"`, not as a dictionary.
- For example, if fixing "nginx:notarealtag", suggest `"nginx:1.21"` or another real, working public image.


COMMAND FAILURE ANALYSIS:
When you see command failure, determine WHY it's failing and provide a SPECIFIC solution:

1. **If container exits immediately**: Use a long-running command
   - For web apps: ["nginx", "-g", "daemon off;"] or ["python", "-m", "http.server", "8080"]
   - For debugging: ["/bin/sh", "-c", "while true; do echo alive; sleep 30; done"]
   - For apps: ["your-app", "--config", "/etc/config"]

2. **If command not found**: Fix the executable path
   - ["python3", "app.py"] instead of ["python", "app.py"]
   - ["/usr/bin/java", "-jar", "app.jar"] instead of ["java", "-jar", "app.jar"]

3. **If missing config/files**: Add proper startup sequence
   - ["/bin/sh", "-c", "mkdir -p /app/logs && exec python app.py"]

4. **For common applications**:
   - **Nginx**: ["nginx", "-g", "daemon off;"]
   - **Apache**: ["httpd-foreground"]  
   - **Python Flask/Django**: ["python", "app.py"] or ["gunicorn", "--bind", "0.0.0.0:8000", "app:app"]
   - **Node.js**: ["node", "server.js"] or ["npm", "start"]
   - **Java**: ["java", "-jar", "app.jar"]
   - **Go**: ["./main"] or ["/app/myapp"]

EXAMPLES WITH REAL COMMANDS:

For Command Failure (Container exiting):
```json
{{
    "problem_type": "command_failure",
    "problem_analysis": "Container is exiting because the command completes immediately instead of running continuously",
    "validation_required": false,
    "changes": [{{
        "type": "update_command",
        "path": ["0"],
        "value": ["/bin/sh", "-c", "while true; do echo 'Container running...'; sleep 60; done"],
        "description": "Replace exiting command with long-running process"
    }}],
    "expected_impact": "Container should stay running instead of exiting",
    "requires_pr": true
}}
```

For Web Application Command Fix:
```json
{{
    "problem_type": "command_failure", 
    "problem_analysis": "Web application command is incorrect - should use proper web server startup",
    "validation_required": false,
    "changes": [{{
        "type": "update_command",
        "path": ["0"],
        "value": ["python", "-m", "flask", "run", "--host=0.0.0.0", "--port=8080"],
        "description": "Fix Python Flask application startup command"
    }}],
    "expected_impact": "Web application should start and listen on port 8080",
    "requires_pr": true
}}
```

For Nginx Command Fix:
```json
{{
    "problem_type": "command_failure",
    "problem_analysis": "Nginx container is not staying running because daemon mode is enabled",
    "validation_required": false,
    "changes": [{{
        "type": "update_command", 
        "path": ["0"],
        "value": ["nginx", "-g", "daemon off;"],
        "description": "Start nginx in foreground mode for container"
    }}],
    "expected_impact": "Nginx should start and stay running in foreground",
    "requires_pr": true
}}
```

For Database Command Fix:
```json
{{
    "problem_type": "command_failure",
    "problem_analysis": "Database container needs proper startup command with configuration",
    "validation_required": false,
    "changes": [{{
        "type": "update_command",
        "path": ["0"], 
        "value": ["mysqld", "--user=mysql", "--datadir=/var/lib/mysql", "--socket=/var/run/mysqld/mysqld.sock"],
        "description": "Proper MySQL startup command with required parameters"
    }}],
    "expected_impact": "MySQL should start successfully with proper configuration",
    "requires_pr": true
}}
```

For Readiness Probe (when the issue is probe related):
```json
{{
    "problem_type": "readiness_probe_failure",
    "problem_analysis": "Readiness probe is failing because the health endpoint path is incorrect",
    "validation_required": false,
    "changes": [{{
        "type": "update_readiness_probe",
        "path": ["0"],
        "value": {{
            "httpGet": {{"path": "/health", "port": 8080}},
            "initialDelaySeconds": 30,
            "periodSeconds": 10,
            "failureThreshold": 5
        }},
        "description": "Fix readiness probe path and increase failure threshold"
    }}],
    "expected_impact": "Pod should pass readiness checks once application starts",
    "requires_pr": true
}}
```

For ImagePullBackOff issues:
```json
{{
    "problem_type": "image_pull_backoff",
    "problem_analysis": "Container cannot pull image 'badimage:nonexistent' - image does not exist in registry",
    "validation_required": true,
    "validation_type": "image_check", 
    "validation_data": {{"image": "nginx:1.21"}},
    "changes": [{{
        "type": "update_image",
        "path": ["0"],
        "value": "nginx:1.21",
        "description": "Replace non-existent image with working nginx:1.21"
    }}],
    "expected_impact": "Pod should successfully pull image and start",
    "requires_pr": true
}}
```

For Probe failures:
```json
{{
    "problem_type": "readiness_probe_failure",
    "problem_analysis": "Readiness probe failing on incorrect endpoint or timing",
    "validation_required": false,
    "changes": [{{
        "type": "update_readiness_probe",
        "path": ["0"], 
        "value": {{
            "httpGet": {{"path": "/health", "port": 8080}},
            "initialDelaySeconds": 30,
            "periodSeconds": 10,
            "failureThreshold": 5
        }},
        "description": "Fix readiness probe endpoint and timing"
    }}],
    "expected_impact": "Pod should pass readiness checks",
    "requires_pr": true
}}
```
REAL IMAGE SUGGESTIONS (use these instead of placeholders):
- For nginx issues: "nginx:1.21", "nginx:alpine", "nginx:1.20"
- For apache issues: "httpd:2.4", "httpd:alpine" 
- For python issues: "python:3.9-slim", "python:3.8-alpine"
- For node issues: "node:16-alpine", "node:14-slim"
- For java issues: "openjdk:11-jre-slim", "openjdk:8-alpine"
- For generic issues: "busybox:latest", "alpine:latest"

IMPORTANT:
- Never use placeholder text like "your-fixed-command-here" 
- Always provide REAL, executable commands
- Consider the application type from the manifest/alert context
- If you can't determine the exact command, use a safe long-running command like sleep loop
- Match the command to what makes sense for the container image being used

Analyze the alert details and manifest carefully to provide the EXACT fix needed.
"""

    ollama_payload = {
        "model": "llama3",
        "prompt": ollama_prompt,
        "stream": False
    }

    # Initialize response variables
    rca = "No response from Ollama."
    instructions = None
    pr_url = None
    validation_result = None
    timestamp_utc = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Get Ollama analysis
    try:
        log.info("🧠 Sending request to Ollama for analysis...")
        response = requests.post(OLLAMA_URL, json=ollama_payload, timeout=60)
        response.raise_for_status()
        rca = response.json().get("response", rca)
        
        # Parse instructions from Ollama response
        instructions = parse_ollama_instructions(rca)
        log.info(f"✅ Ollama analysis completed. Instructions parsed: {instructions is not None}")
        
    except Exception as e:
        rca = f"⚠️ Ollama request failed: {e}"
        log.error(rca)

    # Process Ollama instructions
    if instructions:
        log.info(f"📋 Processing instructions:")
        log.info(f"   - Problem Type: {instructions.get('problem_type')}")
        log.info(f"   - Requires PR: {instructions.get('requires_pr')}")
        log.info(f"   - Number of Changes: {len(instructions.get('changes', []))}")
        
        for i, change in enumerate(instructions.get('changes', [])):
            log.info(f"   - Change {i+1}: {change.get('type')} | {change.get('description')}")
        
        # Handle validation if required
        if instructions.get('validation_required') and instructions.get('validation_type') == 'image_check':
            validation_data = instructions.get('validation_data', {})
            image_name = validation_data.get('image')
            if image_name:
                log.info(f"🔍 Validating image: {image_name}")
                is_valid, validation_msg = validate_image_exists(image_name)
                validation_result = f"Image validation: {validation_msg}"
                
                if not is_valid:
                    log.warning(f"⚠️ Image validation failed: {validation_msg}")
                else:
                    log.info(f"✅ Image validation passed: {validation_msg}")

        # Create PR if required and instructions are valid
        if instructions.get('requires_pr') and instructions.get('changes'):
            log.info("🔄 Creating PR as requested by Ollama instructions...")
            try:
                pr_url = create_fix_pr(pod_name, instructions, rca)
                if pr_url:
                    log.info(f"✅ PR created successfully: {pr_url}")
                else:
                    log.warning("⚠️ PR creation returned None")
            except Exception as e:
                log.error(f"❌ Failed to create PR: {e}")
        else:
            if not instructions.get('requires_pr'):
                log.info("ℹ️ Ollama determined no PR is required")
            if not instructions.get('changes'):
                log.warning("⚠️ No changes specified in instructions")
    else:
        log.error("❌ No valid instructions received from Ollama")

    # --- SLACK NOTIFICATION (concise style) ---
    slack_blocks = compose_slack_alert_blocks(
        alerts=alerts,
        instructions=instructions,
        rca=rca,
        pr_url=pr_url,
        validation_result=validation_result
    )

    # Send to Slack
    try:
        slack_response = requests.post(SLACK_WEBHOOK_URL, json={"blocks": slack_blocks})
        slack_response.raise_for_status()
        log.info("✅ Analysis sent to Slack")
    except Exception as e:
        log.warning(f"⚠️ Slack webhook failed: {e}")

    # Return response
    response_data = {
        "status": "ok",
        "pod_name": pod_name,
        "analysis_completed": instructions is not None,
        "pr_created": pr_url is not None,
        "pr_url": pr_url,
        "problem_type": instructions.get('problem_type') if instructions else None
    }

    return jsonify(response_data), 200

@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "service": "k8s-admin-ai"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
