"""
HTML templates rendered by the review/result Flask routes.
Extracted verbatim from app.py (Phase 1 refactor). Re-exported there so
existing references and tests (app_module.X) keep resolving.
"""

REVIEW_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Review: {{ data.title }}</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               background: #f1f5f9; color: #1e293b; padding: 20px; }
        .container { max-width: 800px; margin: 0 auto; }
        .card { background: white; border-radius: 12px; padding: 24px; margin-bottom: 20px;
                box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        h1 { font-size: 24px; margin-bottom: 4px; }
        .subtitle { color: #64748b; font-size: 14px; margin-bottom: 20px; }
        h2 { font-size: 18px; margin-bottom: 16px; color: #334155; }
        .signal-badge { display: inline-block; padding: 4px 12px; border-radius: 20px;
                        font-size: 12px; font-weight: 600; margin-right: 6px; margin-bottom: 6px; }
        .high { background: #dcfce7; color: #166534; }
        .medium { background: #fef3c7; color: #92400e; }
        .low { background: #fee2e2; color: #991b1b; }
        .task-item { border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px;
                     margin-bottom: 12px; position: relative; }
        .task-item.deleted { opacity: 0.4; text-decoration: line-through; }
        .task-header { display: flex; justify-content: space-between; align-items: flex-start; }
        textarea { width: 100%; border: 1px solid #cbd5e1; border-radius: 6px; padding: 10px;
                   font-family: inherit; font-size: 14px; resize: vertical; min-height: 60px; }
        textarea:focus { outline: none; border-color: #3b82f6; box-shadow: 0 0 0 3px rgba(59,130,246,0.1); }
        input[type=text] { width: 100%; border: 1px solid #cbd5e1; border-radius: 6px;
                           padding: 8px 10px; font-family: inherit; font-size: 14px; }
        input[type=text]:focus { outline: none; border-color: #3b82f6; }
        label { font-size: 12px; font-weight: 600; color: #64748b; display: block; margin-bottom: 4px; margin-top: 10px; }
        .btn { padding: 10px 20px; border-radius: 8px; border: none; font-size: 14px;
               font-weight: 600; cursor: pointer; transition: all 0.2s; }
        .btn-primary { background: #2563eb; color: white; }
        .btn-primary:hover { background: #1d4ed8; }
        .btn-danger { background: #fee2e2; color: #dc2626; }
        .btn-danger:hover { background: #fecaca; }
        .btn-ghost { background: transparent; color: #64748b; border: 1px solid #e2e8f0; }
        .btn-ghost:hover { background: #f8fafc; }
        .btn-sm { padding: 6px 12px; font-size: 12px; }
        .actions-bar { display: flex; justify-content: space-between; align-items: center;
                       margin-top: 20px; padding-top: 20px; border-top: 1px solid #e2e8f0; }
        .email-preview { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px;
                         padding: 16px; margin-top: 12px; }
        .meta-row { display: flex; gap: 8px; margin-bottom: 6px; font-size: 13px; }
        .meta-label { font-weight: 600; color: #64748b; min-width: 70px; }
        .signals-list { list-style: none; padding: 0; }
        .signals-list li { padding: 6px 0; font-size: 14px; }
            .signals-list li::before { content: "-> "; color: #3b82f6; font-weight: bold; }
        .status-banner { padding: 16px; border-radius: 8px; text-align: center; font-weight: 600; }
        .status-executed { background: #dcfce7; color: #166534; }
        .status-pending { background: #dbeafe; color: #1e40af; }
    </style>
</head>
<body>
    <div class="container">
        {% if data.status == 'executed' %}
        <div class="card status-banner status-executed">
              Actions already executed for this meeting.
        </div>
        {% endif %}

        <div class="card">
        <h1>{{ data.title }}</h1>
        <p class="subtitle">{{ (data.meeting_date|string)[:10] }} | Organizer: {{ data.organizer_email }}</p>

            {% set signals = data.intelligence.get('signals', {}) %}
            <div style="margin-top: 12px;">
                <span class="signal-badge {{ signals.get('interest_level', 'medium') }}">
                    {{ signals.get('interest_level', 'medium')|upper }} INTEREST
                </span>
                <span class="signal-badge" style="background:#ede9fe;color:#5b21b6;">
                    {{ signals.get('relationship_type', 'other')|upper }}
                </span>
            </div>

            {% if signals.get('key_signals') %}
            <div style="margin-top: 16px;">
                <label>KEY SIGNALS</label>
                <ul class="signals-list">
                    {% for s in signals.key_signals %}
                    <li>{{ s }}</li>
                    {% endfor %}
                </ul>
            </div>
            {% endif %}
        </div>

        <form method="POST" action="/review/{{ data.id }}/approve">
            <!-- ACTION ITEMS -->
            <div class="card">
        <h2>Action Items</h2>
                <p style="color:#64748b; font-size:13px; margin-bottom:16px;">
                    Edit task text, change owners, or delete tasks you don't need. Only approved tasks will be created.
                </p>

                {% for item in data.intelligence.get('action_items', []) %}
                <div class="task-item" id="task-{{ loop.index0 }}">
                    <div class="task-header">
                        <div style="flex:1;">
                            <label>TASK</label>
                            <textarea name="task_text_{{ loop.index0 }}" rows="2">{{ item.task }}</textarea>

                            <div style="display:flex; gap:12px;">
                                <div style="flex:1;">
                                    <label>OWNER</label>
                                    <select name="task_owner_email_{{ loop.index0 }}" style="width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;">
                                        {% for member in team_members %}
                                        <option value="{{ member.email }}" {{ 'selected' if member.email == item.get('owner_email', '') or member.name == item.get('owner', '') }}>{{ member.name }}</option>
                                        {% endfor %}
                                        {% if item.get('owner_email') and item.get('owner_email') not in team_members|map(attribute='email')|list and item.get('owner', '') not in team_members|map(attribute='name')|list %}
                                        <option value="{{ item.get('owner_email', '') }}" selected>{{ item.get('owner', item.get('owner_email', 'Unknown')) }}</option>
                                        {% endif %}
                                    </select>
                                    <input type="hidden" name="task_owner_{{ loop.index0 }}" value="{{ item.get('owner', '') }}">
                                </div>
                                <div style="flex:0.5;">
                                    <label>CREATE IN</label>
                                    <select name="task_create_in_{{ loop.index0 }}" style="width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;">
                                        <option value="hubspot" {{ 'selected' if item.get('create_in') == 'hubspot' }}>HubSpot (external)</option>
                                        <option value="asana" {{ 'selected' if item.get('create_in', 'asana') == 'asana' }}>Asana (internal)</option>
                                        <option value="both" {{ 'selected' if item.get('create_in') == 'both' }}>Both</option>
                                    </select>
                                </div>
                                <div style="flex:0.5;">
                                    <label>PRIORITY</label>
                                    <select name="task_priority_{{ loop.index0 }}" style="width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;">
                                        <option value="high" {{ 'selected' if item.get('priority') == 'high' }}>High</option>
                                        <option value="medium" {{ 'selected' if item.get('priority', 'medium') == 'medium' }}>Medium</option>
                                        <option value="low" {{ 'selected' if item.get('priority') == 'low' }}>Low</option>
                                    </select>
                                </div>
                                <div style="flex:0.5;">
                                    <label>DUE (days)</label>
                                    <input type="number" name="task_due_days_{{ loop.index0 }}" value="{{ item.get('due_days', 7) }}" min="1" max="90"
                                           style="width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;">
                                    <span style="font-size:11px;color:#94a3b8;">{{ item.get('due_context', '') }}</span>
                                </div>
                            </div>
                        </div>
                        <div style="margin-left:12px; padding-top:20px;">
                            <label style="display:flex; align-items:center; gap:6px; cursor:pointer;">
                                <input type="checkbox" name="task_delete_{{ loop.index0 }}" value="1"
                                       onchange="this.closest('.task-item').classList.toggle('deleted')">
                                <span style="font-size:12px; color:#dc2626;">Delete</span>
                            </label>
                        </div>
                    </div>
                </div>
                {% endfor %}

                <input type="hidden" name="task_count" value="{{ data.intelligence.get('action_items', [])|length }}">
            </div>

            <!-- FOLLOW-UP EMAIL -->
            <div class="card">
        <h2>Follow-Up Email Draft</h2>
                <p style="color:#64748b; font-size:13px; margin-bottom:16px;">
                    This will be saved as a draft in Outlook ({{ data.organizer_email }}). Edit before approving.
                </p>

                {% set email = data.intelligence.get('follow_up_email', {}) %}
                <label>TO</label>
                <input type="text" name="email_to" value="{% for r in email.get('to_recipients', []) %}{{ r.get('name', '') }} &lt;{{ r.get('email', '') }}&gt;{% if not loop.last %}, {% endif %}{% endfor %}">

                <label>SUBJECT</label>
                <input type="text" name="email_subject" value="{{ email.get('subject', '') }}">

                <label>BODY</label>
                <textarea name="email_body" rows="10">{{ email.get('body_text', '') }}</textarea>

                <label style="display:flex; align-items:center; gap:6px; margin-top:12px; cursor:pointer;">
                    <input type="checkbox" name="skip_email" value="1">
                    <span style="font-size:13px; color:#64748b;">Skip email draft  --  don't create in Outlook</span>
                </label>
            </div>

            <!-- APPROVE / CANCEL -->
            {% if data.status == 'pending' %}
            <div class="card">
                <div class="actions-bar" style="border-top:none; margin-top:0; padding-top:0;">
                    <a href="/review/{{ data.id }}/cancel" class="btn btn-ghost">Cancel  --  Don't Create Anything</a>
                    <button type="submit" class="btn btn-primary"> Approve & Create Tasks + Draft</button>
                </div>
            </div>
            {% endif %}
        </form>
        <script>
        // Sync hidden owner name when dropdown changes
        document.querySelectorAll('select[name^="task_owner_email_"]').forEach(function(sel) {
            sel.addEventListener('change', function() {
                var idx = this.name.replace('task_owner_email_', '');
                var nameField = document.querySelector('input[name="task_owner_' + idx + '"]');
                if (nameField) {
                    nameField.value = this.options[this.selectedIndex].text;
                }
            });
            // Initialize name on page load
            var idx = sel.name.replace('task_owner_email_', '');
            var nameField = document.querySelector('input[name="task_owner_' + idx + '"]');
            if (nameField && sel.selectedIndex >= 0) {
                nameField.value = sel.options[sel.selectedIndex].text;
            }
        });
        </script>
    </div>
</body>
</html>
"""

RESULT_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ status_title }}</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #f1f5f9;
               color: #1e293b; padding: 20px; }
        .container { max-width: 600px; margin: 40px auto; }
        .card { background: white; border-radius: 12px; padding: 32px; text-align: center;
                box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        h1 { font-size: 24px; margin-bottom: 12px; }
        .action-item { text-align: left; padding: 8px 0; font-size: 14px; border-bottom: 1px solid #f1f5f9; }
        .action-item:last-child { border-bottom: none; }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <h1>{{ status_emoji }} {{ status_title }}</h1>
            <p style="color:#64748b; margin-bottom:24px;">{{ data.title }}</p>
            {% if actions %}
            <div style="text-align:left; margin-top:20px;">
                {% for action in actions %}
                <div class="action-item">{{ action }}</div>
                {% endfor %}
            </div>
            {% endif %}
        </div>
    </div>
</body>
</html>
"""
