/**
 * Browser Agent - Two-Pane UI
 * Main JavaScript file with state management and timeline rendering
 */

document.addEventListener('DOMContentLoaded', function() {
    // ============================================
    // State Management
    // ============================================
    const WorkflowState = {
        IDLE: 'idle',
        RUNNING: 'running',
        DONE: 'done',
        FAILED: 'failed'
    };

    let workflowState = WorkflowState.IDLE;

    // ============================================
    // DOM Elements
    // ============================================
    const instructionsInput = document.getElementById('instructions');
    const sendBtn = document.getElementById('send-btn');
    const attachBtn = document.getElementById('attach-btn');
    const resumeInput = document.getElementById('resume');
    const jobDescInput = document.getElementById('job-description');
    const timeline = document.getElementById('timeline');
    const emptyState = document.getElementById('empty-state');
    const screenshotViewer = document.getElementById('screenshot-viewer');
    const viewerImage = document.getElementById('viewer-image');
    const viewerCaption = document.getElementById('viewer-caption');
    const errorToast = document.getElementById('error');

    // User data fields
    const userFirstNameInput = document.getElementById('user-first-name');
    const userLastNameInput = document.getElementById('user-last-name');
    const userEmailInput = document.getElementById('user-email');
    const userPhoneInput = document.getElementById('user-phone');
    const userLocationInput = document.getElementById('user-location');
    const userLinkedinInput = document.getElementById('user-linkedin');

    // Suggestion cards
    const suggestionCards = document.querySelectorAll('.suggestion-card');

    // ============================================
    // Workflow Presets
    // ============================================
    const WORKFLOW_PRESETS = {
        greenhouse: {
            instructions: `Go to https://job-boards.greenhouse.io/anthropic/jobs/5026017008
Wait 2 seconds
Fill the application form with user profile
Take a screenshot`,
            prefillUserData: true
        },
        extract: {
            instructions: `Go to https://boards.greenhouse.io/stripe
Wait 1 second
Extract job titles using ".opening a"
Take a screenshot`,
            prefillUserData: false
        },
        click: {
            instructions: `Go to https://boards.greenhouse.io/stripe
Wait 1 second
Click the first job listing
Wait 1 second
Take a screenshot`,
            prefillUserData: false
        },
        test: {
            instructions: `Go to https://example.com
Wait 2 seconds
Take a screenshot`,
            prefillUserData: false
        },
        scrape_greenhouse: {
            workflow_json: [
                {
                    "action": "goto",
                    "url": "https://boards.greenhouse.io/anthropic/jobs/4020056008"
                },
                {
                    "action": "wait",
                    "duration": 1500
                },
                {
                    "action": "extract",
                    "selector": "h1",
                    "extract_mode": "text"
                },
                {
                    "action": "extract",
                    "selector": ".location",
                    "extract_mode": "text"
                },
                {
                    "action": "extract",
                    "selector": "#content",
                    "extract_mode": "text"
                },
                {
                    "action": "screenshot"
                }
            ],
            description: "Extract title, location & job description",
            prefillUserData: false
        }
    };

    // Demo user data for Greenhouse preset
    const DEMO_USER_DATA = {
        first_name: 'Test',
        last_name: 'User',
        email: 'test@example.com',
        phone: '555-123-4567',
        location: 'San Francisco, CA',
        linkedin_url: 'https://linkedin.com/in/testuser'
    };

    // ============================================
    // State Management Functions
    // ============================================
    function setWorkflowState(state) {
        workflowState = state;
        updateUIState();
    }

    function updateUIState() {
        const isRunning = workflowState === WorkflowState.RUNNING;

        // Disable/enable suggestion cards
        suggestionCards.forEach(card => {
            card.disabled = isRunning;
        });

        // Disable/enable input area
        instructionsInput.disabled = isRunning;
        sendBtn.disabled = isRunning;
        attachBtn.disabled = isRunning;

        // Update send button visual state
        if (isRunning) {
            sendBtn.classList.add('loading');
        } else {
            sendBtn.classList.remove('loading');
        }
    }

    // ============================================
    // Timeline Rendering Functions
    // ============================================
    function clearTimeline() {
        timeline.innerHTML = '';
    }

    function hideEmptyState() {
        if (emptyState) {
            emptyState.style.display = 'none';
        }
    }

    function showEmptyState() {
        if (emptyState) {
            emptyState.style.display = 'flex';
        }
    }

    function addTimelineCard(type, content) {
        hideEmptyState();
        const card = document.createElement('div');
        card.className = `timeline-card ${type}`;
        card.innerHTML = content;
        timeline.appendChild(card);

        // Auto-scroll to the new card
        requestAnimationFrame(() => {
            card.scrollIntoView({ behavior: 'smooth', block: 'end' });
        });

        return card;
    }

    function showTimelineLoading() {
        hideEmptyState();
        const content = `
            <div class="loading-spinner"></div>
            <p class="loading-text">Executing workflow...</p>
        `;
        return addTimelineCard('loading-card', content);
    }

    function removeTimelineLoading() {
        const loadingCard = timeline.querySelector('.loading-card');
        if (loadingCard) {
            loadingCard.remove();
        }
    }

    function renderWorkflowParsed(steps) {
        const content = `
            <div class="card-header">
                <span class="card-icon">📋</span>
                <span class="card-title">Workflow Parsed</span>
            </div>
            <pre class="json-display">${escapeHtml(JSON.stringify(steps, null, 2))}</pre>
        `;
        addTimelineCard('workflow-parsed', content);
    }

    function renderStepExecution(step, index) {
        const statusClass = step.status === 'success' ? 'success' : 'failed';
        const statusIcon = step.status === 'success' ? '✓' : '✗';
        const stepNum = step.step_number !== undefined ? step.step_number : index;

        let logsHtml = '';
        if (step.logs && step.logs.length > 0) {
            logsHtml = `
                <div class="step-logs">
                    ${step.logs.map(log => `<div class="log-line">${escapeHtml(log)}</div>`).join('')}
                </div>
            `;
        }

        let errorHtml = '';
        if (step.error) {
            errorHtml = `<div class="step-error">${escapeHtml(step.error)}</div>`;
        }

        // Display extracted data if present
        let extractedHtml = '';
        if (step.extracted_data) {
            if (Array.isArray(step.extracted_data)) {
                extractedHtml = `
                    <div class="step-logs">
                        <div class="log-line" style="color: var(--accent-cyan); font-weight: 500;">
                            Extracted ${step.extracted_data.length} items:
                        </div>
                        ${step.extracted_data.slice(0, 10).map(item =>
                            `<div class="log-line">${escapeHtml(String(item))}</div>`
                        ).join('')}
                        ${step.extracted_data.length > 10 ?
                            `<div class="log-line" style="color: var(--neutral-500);">... and ${step.extracted_data.length - 10} more</div>` : ''
                        }
                    </div>
                `;
            } else {
                extractedHtml = `
                    <div class="step-logs">
                        <div class="log-line" style="color: var(--accent-cyan); font-weight: 500;">Extracted Data:</div>
                        <div class="log-line">${escapeHtml(String(step.extracted_data).substring(0, 500))}</div>
                    </div>
                `;
            }
        }

        const content = `
            <div class="card-header">
                <span class="step-badge">Step ${stepNum}</span>
                <span class="action-name">${escapeHtml(step.action || 'unknown')}</span>
                <span class="status-pill ${statusClass}">${statusIcon} ${step.status}</span>
                <span class="duration">${step.duration_ms || 0}ms</span>
            </div>
            ${logsHtml}
            ${extractedHtml}
            ${errorHtml}
        `;

        addTimelineCard(`step-card ${statusClass}`, content);
    }

    function renderScreenshot(step, index) {
        if (!step.screenshot_base64 || step.screenshot_base64.length < 100) return;

        let imgSrc = step.screenshot_base64;
        if (!imgSrc.startsWith('data:')) {
            // Detect image format from base64 header
            if (imgSrc.startsWith('/9j/')) {
                imgSrc = `data:image/jpeg;base64,${imgSrc}`;
            } else if (imgSrc.startsWith('iVBOR')) {
                imgSrc = `data:image/png;base64,${imgSrc}`;
            } else {
                imgSrc = `data:image/png;base64,${imgSrc}`;
            }
        }

        const stepNum = step.step_number !== undefined ? step.step_number : index;
        const action = step.action || 'screenshot';

        const content = `
            <img src="${imgSrc}" alt="Step ${stepNum} screenshot" onclick="openScreenshotViewer(this.src, 'Step ${stepNum}: ${escapeHtml(action)}')" />
            <div class="caption">Step ${stepNum}: ${escapeHtml(action)}</div>
        `;

        addTimelineCard('screenshot-card', content);
    }

    function renderFinalStatus(data) {
        const success = data.success !== false;
        const statusClass = success ? 'success' : 'failed';
        const statusIcon = success ? '✓' : '✗';
        const statusText = success ? 'COMPLETED' : 'FAILED';

        const content = `
            <div class="final-status-header">
                <span class="status-badge-large ${statusClass}">${statusIcon} ${statusText}</span>
            </div>
            <div class="final-status-details">
                <span>Workflow ID: <strong>${data.workflow_id || 'N/A'}</strong></span>
                <span>Total Duration: <strong>${data.total_duration_ms || 0}ms</strong></span>
                <span>Steps: <strong>${(data.steps || []).length}</strong></span>
            </div>
        `;

        addTimelineCard(`final-status ${statusClass}`, content);
    }

    // ============================================
    // Workflow Execution (Streaming with SSE)
    // ============================================
    let eventSource = null;

    function runWorkflow() {
        const instructions = instructionsInput.value.trim();

        if (!instructions) {
            showError('Please enter workflow instructions');
            return;
        }

        // Close any existing connection
        if (eventSource) {
            eventSource.close();
            eventSource = null;
        }

        setWorkflowState(WorkflowState.RUNNING);
        clearTimeline();
        hideError();

        // Show loading state in timeline
        showTimelineLoading();

        // Collect user data
        const userData = collectUserData();

        // Build streaming URL with query parameters
        const params = new URLSearchParams({
            instructions: instructions,
            user_data: JSON.stringify(userData)
        });

        const streamUrl = `/api/workflow/run-stream?${params.toString()}`;

        // Create EventSource for SSE
        eventSource = new EventSource(streamUrl);

        // Track workflow data for final status
        let workflowId = null;
        let stepCount = 0;

        // Handle status messages
        eventSource.addEventListener('status', (event) => {
            const data = JSON.parse(event.data);
            console.log('[SSE] Status:', data.message);
            // Could update loading text here if needed
        });

        // Handle workflow parsed event
        eventSource.addEventListener('workflow_parsed', (event) => {
            const data = JSON.parse(event.data);
            console.log('[SSE] Workflow parsed:', data);

            workflowId = data.workflow_id;
            stepCount = data.count;

            // Remove loading card and show parsed workflow
            removeTimelineLoading();
            renderWorkflowParsed(data.steps);
        });

        // Handle step start event
        eventSource.addEventListener('step_start', (event) => {
            const data = JSON.parse(event.data);
            console.log('[SSE] Step start:', data);

            // Add a "running" indicator card for this step
            addStepRunningCard(data.step_number, data.action, data.total_steps);
        });

        // Handle step complete event
        eventSource.addEventListener('step_complete', (event) => {
            const data = JSON.parse(event.data);
            console.log('[SSE] Step complete:', data);

            // Remove the running card
            removeStepRunningCard(data.step_number);

            // Render the completed step
            renderStepExecution(data, data.step_number);
            renderScreenshot(data, data.step_number);
        });

        // Handle workflow complete event
        eventSource.addEventListener('workflow_complete', (event) => {
            const data = JSON.parse(event.data);
            console.log('[SSE] Workflow complete:', data);

            // Render final status
            renderFinalStatus({
                workflow_id: data.workflow_id,
                success: data.success,
                total_duration_ms: data.total_duration_ms,
                steps: { length: data.steps_completed }
            });

            setWorkflowState(data.success ? WorkflowState.DONE : WorkflowState.FAILED);

            // Close the connection
            eventSource.close();
            eventSource = null;
        });

        // Handle errors
        eventSource.addEventListener('error', (event) => {
            if (event.data) {
                const data = JSON.parse(event.data);
                console.error('[SSE] Error event:', data);
                showError('Workflow error: ' + data.error);
            }
        });

        // Handle connection errors
        eventSource.onerror = (error) => {
            console.error('[SSE] Connection error:', error);

            // Only show error if we haven't completed yet
            if (workflowState === WorkflowState.RUNNING) {
                removeTimelineLoading();
                showError('Connection lost. Please try again.');
                setWorkflowState(WorkflowState.FAILED);
            }

            eventSource.close();
            eventSource = null;
        };
    }

    // Add a "running" card while step is executing
    function addStepRunningCard(stepNumber, action, totalSteps) {
        const content = `
            <div class="card-header">
                <span class="step-badge">Step ${stepNumber}</span>
                <span class="action-name">${escapeHtml(action)}</span>
                <span class="status-pill" style="background: var(--warning-bg); color: var(--warning);">
                    <span class="loading-spinner" style="width: 12px; height: 12px; border-width: 2px; margin-right: 4px;"></span>
                    running
                </span>
                <span class="duration">${stepNumber + 1} of ${totalSteps}</span>
            </div>
            <div class="step-logs">
                <div class="log-line">Executing ${escapeHtml(action)}...</div>
            </div>
        `;
        const card = addTimelineCard(`step-card running step-running-${stepNumber}`, content);
        return card;
    }

    // Remove the "running" card when step completes
    function removeStepRunningCard(stepNumber) {
        const runningCard = timeline.querySelector(`.step-running-${stepNumber}`);
        if (runningCard) {
            runningCard.remove();
        }
    }

    function collectUserData() {
        const userData = {};
        if (userFirstNameInput && userFirstNameInput.value.trim()) {
            userData.first_name = userFirstNameInput.value.trim();
        }
        if (userLastNameInput && userLastNameInput.value.trim()) {
            userData.last_name = userLastNameInput.value.trim();
        }
        if (userEmailInput && userEmailInput.value.trim()) {
            userData.email = userEmailInput.value.trim();
        }
        if (userPhoneInput && userPhoneInput.value.trim()) {
            userData.phone = userPhoneInput.value.trim();
        }
        if (userLocationInput && userLocationInput.value.trim()) {
            userData.location = userLocationInput.value.trim();
        }
        if (userLinkedinInput && userLinkedinInput.value.trim()) {
            userData.linkedin_url = userLinkedinInput.value.trim();
        }
        return userData;
    }

    function prefillDemoUserData() {
        if (userFirstNameInput && !userFirstNameInput.value) {
            userFirstNameInput.value = DEMO_USER_DATA.first_name;
        }
        if (userLastNameInput && !userLastNameInput.value) {
            userLastNameInput.value = DEMO_USER_DATA.last_name;
        }
        if (userEmailInput && !userEmailInput.value) {
            userEmailInput.value = DEMO_USER_DATA.email;
        }
        if (userPhoneInput && !userPhoneInput.value) {
            userPhoneInput.value = DEMO_USER_DATA.phone;
        }
        if (userLocationInput && !userLocationInput.value) {
            userLocationInput.value = DEMO_USER_DATA.location;
        }
        if (userLinkedinInput && !userLinkedinInput.value) {
            userLinkedinInput.value = DEMO_USER_DATA.linkedin_url;
        }

        // Open the user data panel so user can see pre-filled data
        const userDataPanel = document.querySelector('.user-data-panel');
        if (userDataPanel) {
            userDataPanel.open = true;
        }
    }

    // ============================================
    // Screenshot Viewer
    // ============================================
    window.openScreenshotViewer = function(src, caption) {
        viewerImage.src = src;
        viewerCaption.textContent = caption || '';
        screenshotViewer.classList.add('active');
        document.body.style.overflow = 'hidden';
    };

    window.closeScreenshotViewer = function() {
        screenshotViewer.classList.remove('active');
        document.body.style.overflow = '';
    };

    // ============================================
    // Error Handling
    // ============================================
    function showError(message) {
        errorToast.textContent = message;
        errorToast.classList.add('visible');

        // Auto-hide after 5 seconds
        setTimeout(() => {
            hideError();
        }, 5000);
    }

    function hideError() {
        errorToast.classList.remove('visible');
    }

    // ============================================
    // Utility Functions
    // ============================================
    function escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // ============================================
    // Event Listeners
    // ============================================

    // Send button click
    sendBtn.addEventListener('click', runWorkflow);

    // Enter key in textarea (with Cmd/Ctrl)
    instructionsInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            runWorkflow();
        }
    });

    // Attach button triggers file input
    attachBtn.addEventListener('click', () => {
        resumeInput.click();
    });

    // File input change handler
    resumeInput.addEventListener('change', () => {
        if (resumeInput.files.length > 0) {
            attachBtn.style.color = 'var(--accent-cyan)';
            attachBtn.title = `Attached: ${resumeInput.files[0].name}`;
        } else {
            attachBtn.style.color = '';
            attachBtn.title = 'Attach file';
        }
    });

    // Suggestion card click handlers
    suggestionCards.forEach(card => {
        card.addEventListener('click', () => {
            const workflowKey = card.dataset.workflow;
            const preset = WORKFLOW_PRESETS[workflowKey];

            if (preset) {
                // Check if preset uses workflow_json (Builder mode)
                if (preset.workflow_json) {
                    // Switch to Builder mode
                    switchMode('builder');

                    // Load workflow JSON into builderSteps (deep copy)
                    builderSteps = JSON.parse(JSON.stringify(preset.workflow_json));

                    // Render the step cards
                    renderStepsList();

                    // Save to localStorage
                    saveBuilderState();

                    return;
                }

                // Original behavior: populate instructions textarea
                instructionsInput.value = preset.instructions;
                instructionsInput.focus();

                if (preset.prefillUserData) {
                    prefillDemoUserData();
                }
            }
        });
    });

    // Close screenshot viewer on escape key
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeScreenshotViewer();
        }
    });

    // ============================================
    // Initialize
    // ============================================
    updateUIState();

    // ============================================
    // WORKFLOW BUILDER
    // ============================================

    // Builder State
    let builderSteps = [];
    let currentMode = 'chat';
    let expandedStepIndex = null;
    let draggedStepIndex = null;

    // SVG Icons for step types
    const STEP_ICONS = {
        goto: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>',
        click: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 9h.01"/><path d="m15 9-6 6"/><path d="M11 15h.01"/><path d="M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z"/><path d="m9 9 .01.01"/></svg>',
        type: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7V4h16v3"/><path d="M9 20h6"/><path d="M12 4v16"/></svg>',
        wait: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
        scroll: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v18"/><path d="m8 7-4 4 4 4"/><path d="m16 7 4 4-4 4"/></svg>',
        extract: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" x2="12" y1="3" y2="15"/></svg>',
        screenshot: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/></svg>',
        fill_form: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4Z"/></svg>',
        upload: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>'
    };

    // Step Schema - defines all supported actions and their fields
    const STEP_SCHEMA = {
        goto: {
            label: 'Go to URL',
            icon: STEP_ICONS.goto,
            category: 'Navigate',
            description: 'Navigate to a webpage',
            fields: [
                { name: 'url', type: 'url', label: 'URL', required: true, placeholder: 'https://example.com', hint: 'Enter the full URL including https://' }
            ]
        },
        click: {
            label: 'Click Element',
            icon: STEP_ICONS.click,
            category: 'Interact',
            description: 'Click an element on the page',
            fields: [
                { name: 'selector', type: 'text', label: 'CSS Selector', required: true, placeholder: 'button.submit, #login-btn', hint: 'CSS selector for the element to click' },
                { name: 'auto_detect', type: 'toggle', label: 'Auto-detect element', default: false }
            ]
        },
        type: {
            label: 'Type Text',
            icon: STEP_ICONS.type,
            category: 'Interact',
            description: 'Type text into an input field',
            fields: [
                { name: 'selector', type: 'text', label: 'CSS Selector', required: true, placeholder: 'input#email', hint: 'CSS selector for the input field' },
                { name: 'value', type: 'text', label: 'Text to type', required: true, placeholder: 'Hello world', hint: 'Use {{user.field}} for dynamic values' }
            ]
        },
        wait: {
            label: 'Wait',
            icon: STEP_ICONS.wait,
            category: 'Control',
            description: 'Pause execution for specified time',
            fields: [
                { name: 'duration', type: 'number', label: 'Duration (ms)', required: true, default: 1000, min: 0, placeholder: '1000', hint: '1000ms = 1 second' }
            ]
        },
        scroll: {
            label: 'Scroll Page',
            icon: STEP_ICONS.scroll,
            category: 'Navigate',
            description: 'Scroll page by pixels, to an element, or until text is found',
            fields: [
                { name: 'scroll_mode', type: 'select', label: 'Scroll Mode', options: ['pixels', 'to_element', 'until_text'], default: 'pixels', hint: 'How to determine scroll behavior' },
                { name: 'scroll_direction', type: 'select', label: 'Direction', options: ['down', 'up'], default: 'down', showWhen: { scroll_mode: 'pixels' }, hint: 'Scroll up or down' },
                { name: 'scroll_amount', type: 'number', label: 'Amount (pixels)', default: 500, min: 0, placeholder: '500', showWhen: { scroll_mode: 'pixels' }, hint: 'Distance to scroll in pixels' },
                { name: 'selector', type: 'text', label: 'Target Element', required: true, placeholder: '#footer, .section', showWhen: { scroll_mode: 'to_element' }, hint: 'CSS selector of element to scroll into view' },
                { name: 'scroll_text', type: 'text', label: 'Text to Find', required: true, placeholder: 'Load more...', showWhen: { scroll_mode: 'until_text' }, hint: 'Scroll until this text appears on page' }
            ]
        },
        extract: {
            label: 'Extract Data',
            icon: STEP_ICONS.extract,
            category: 'Extract',
            description: 'Extract text or attributes from elements',
            fields: [
                { name: 'selector', type: 'text', label: 'CSS Selector', required: true, placeholder: '.job-title, h1', hint: 'Matches all elements with this selector' },
                { name: 'extract_mode', type: 'select', label: 'Extract mode', options: ['text', 'attribute'], default: 'text' },
                { name: 'attribute', type: 'text', label: 'Attribute name', required: false, placeholder: 'href, data-id', showWhen: { extract_mode: 'attribute' }, hint: 'e.g., href, src, data-id' }
            ]
        },
        screenshot: {
            label: 'Take Screenshot',
            icon: STEP_ICONS.screenshot,
            category: 'Control',
            description: 'Capture a screenshot of the current page',
            fields: []
        },
        fill_form: {
            label: 'Fill Form',
            icon: STEP_ICONS.fill_form,
            category: 'Forms',
            description: 'Fill multiple form fields at once',
            fields: [
                { name: 'auto_detect', type: 'toggle', label: 'Auto-detect fields from user data', default: true },
                { name: 'fields', type: 'key-value', label: 'Field mappings', required: false, hint: 'Map field names to values or {{user.field}}' }
            ]
        },
        upload: {
            label: 'Upload File',
            icon: STEP_ICONS.upload,
            category: 'Interact',
            description: 'Upload a file to a file input',
            fields: [
                { name: 'selector', type: 'text', label: 'File input selector', required: true, placeholder: 'input[type="file"]', hint: 'CSS selector for the file input' },
                { name: 'file', type: 'text', label: 'Filename', required: true, placeholder: 'resume.pdf', hint: 'Name of the file to upload' }
            ]
        }
    };

    // Preset workflow templates
    const WORKFLOW_TEMPLATES = {
        'scrape-jobs': {
            name: 'Scrape Job Listings',
            description: 'Extract job titles from a career page',
            steps: [
                { action: 'goto', url: 'https://boards.greenhouse.io/stripe' },
                { action: 'wait', duration: 2000 },
                { action: 'extract', selector: '.opening a', extract_mode: 'text' },
                { action: 'screenshot' }
            ]
        },
        'fill-application': {
            name: 'Fill Job Application',
            description: 'Auto-fill a Greenhouse application form',
            steps: [
                { action: 'goto', url: 'https://job-boards.greenhouse.io/example' },
                { action: 'wait', duration: 2000 },
                { action: 'fill_form', auto_detect: true },
                { action: 'screenshot' }
            ]
        },
        'click-navigation': {
            name: 'Click Through Pages',
            description: 'Navigate and click through multiple pages',
            steps: [
                { action: 'goto', url: 'https://example.com' },
                { action: 'wait', duration: 1000 },
                { action: 'click', selector: 'a.next' },
                { action: 'wait', duration: 1000 },
                { action: 'screenshot' }
            ]
        },
        'quick-screenshot': {
            name: 'Quick Screenshot',
            description: 'Take a screenshot of any page',
            steps: [
                { action: 'goto', url: 'https://example.com' },
                { action: 'wait', duration: 2000 },
                { action: 'screenshot' }
            ]
        }
    };

    // Category groupings
    const STEP_CATEGORIES = {
        'Navigate': ['goto', 'scroll'],
        'Interact': ['click', 'type', 'upload'],
        'Extract': ['extract'],
        'Forms': ['fill_form'],
        'Control': ['wait', 'screenshot']
    };

    // Recommended actions for the "Recommended" section
    const RECOMMENDED_STEPS = ['goto', 'click', 'extract', 'fill_form'];

    // Builder DOM Elements
    const modeTabs = document.querySelectorAll('.mode-tab');
    const chatMode = document.getElementById('chat-mode');
    const builderMode = document.getElementById('builder-mode');
    const stepsList = document.getElementById('steps-list');
    const emptySteps = document.getElementById('empty-steps');
    const addStepBtn = document.getElementById('add-step-btn');
    const clearWorkflowBtn = document.getElementById('clear-workflow-btn');
    const runBuilderBtn = document.getElementById('run-builder-btn');
    const previewJsonBtn = document.getElementById('preview-json-btn');
    const stepPickerModal = document.getElementById('step-picker-modal');
    const stepSearchInput = document.getElementById('step-search');
    const jsonPreviewModal = document.getElementById('json-preview-modal');
    const jsonPreviewContent = document.getElementById('json-preview-content');

    // Builder user data fields
    const builderFirstName = document.getElementById('builder-first-name');
    const builderLastName = document.getElementById('builder-last-name');
    const builderEmail = document.getElementById('builder-email');
    const builderPhone = document.getElementById('builder-phone');
    const builderLocation = document.getElementById('builder-location');
    const builderLinkedin = document.getElementById('builder-linkedin');

    // ============================================
    // Mode Switching
    // ============================================
    function switchMode(mode) {
        currentMode = mode;

        modeTabs.forEach(tab => {
            tab.classList.toggle('active', tab.dataset.mode === mode);
        });

        if (mode === 'chat') {
            chatMode.classList.remove('hidden');
            builderMode.classList.add('hidden');
        } else {
            chatMode.classList.add('hidden');
            builderMode.classList.remove('hidden');
            renderStepsList();
        }
    }

    modeTabs.forEach(tab => {
        tab.addEventListener('click', () => {
            switchMode(tab.dataset.mode);
        });
    });

    // "Build Workflow" button in empty state
    const openBuilderBtn = document.getElementById('open-builder-btn');
    if (openBuilderBtn) {
        openBuilderBtn.addEventListener('click', () => {
            switchMode('builder');
        });
    }

    // ============================================
    // Step Picker Modal
    // ============================================
    function openStepPicker() {
        stepPickerModal.classList.remove('hidden');
        stepSearchInput.value = '';
        stepSearchInput.focus();
        renderStepOptions();
    }

    window.closeStepPicker = function() {
        stepPickerModal.classList.add('hidden');
    };

    function renderStepOptions(filter = '') {
        const filterLower = filter.toLowerCase();

        // Render templates section
        const templateContainer = document.getElementById('template-options');
        const templateSection = document.getElementById('templates-section');
        if (templateContainer && templateSection) {
            const filteredTemplates = Object.entries(WORKFLOW_TEMPLATES)
                .filter(([key, template]) => {
                    return !filter ||
                           key.includes(filterLower) ||
                           template.name.toLowerCase().includes(filterLower) ||
                           template.description.toLowerCase().includes(filterLower);
                });

            if (filteredTemplates.length > 0) {
                templateSection.style.display = 'block';
                templateContainer.innerHTML = filteredTemplates
                    .map(([key, template]) => renderTemplateOption(key, template))
                    .join('');

                // Add click handlers for templates
                templateContainer.querySelectorAll('.template-option').forEach(option => {
                    option.addEventListener('click', () => {
                        const templateKey = option.dataset.template;
                        loadTemplate(templateKey);
                        closeStepPicker();
                    });
                });
            } else {
                templateSection.style.display = 'none';
            }
        }

        // Render recommended steps
        const recommendedContainer = document.getElementById('recommended-steps');
        if (recommendedContainer) {
            recommendedContainer.innerHTML = RECOMMENDED_STEPS
                .filter(action => {
                    const schema = STEP_SCHEMA[action];
                    return !filter ||
                           action.includes(filterLower) ||
                           schema.label.toLowerCase().includes(filterLower) ||
                           schema.description.toLowerCase().includes(filterLower);
                })
                .map(action => renderStepOption(action))
                .join('');
        }

        // Render category steps
        Object.entries(STEP_CATEGORIES).forEach(([category, actions]) => {
            const container = document.querySelector(`.step-options[data-category="${category}"]`);
            if (container) {
                container.innerHTML = actions
                    .filter(action => {
                        const schema = STEP_SCHEMA[action];
                        return !filter ||
                               action.includes(filterLower) ||
                               schema.label.toLowerCase().includes(filterLower) ||
                               schema.description.toLowerCase().includes(filterLower);
                    })
                    .map(action => renderStepOption(action))
                    .join('');
            }
        });

        // Add click handlers
        document.querySelectorAll('.step-option').forEach(option => {
            option.addEventListener('click', () => {
                const action = option.dataset.action;
                addStep(action);
                closeStepPicker();
            });
        });
    }

    function renderStepOption(action) {
        const schema = STEP_SCHEMA[action];
        return `
            <button class="step-option" data-action="${action}">
                <span class="step-option-icon">${schema.icon}</span>
                <div class="step-option-info">
                    <span class="step-option-name">${schema.label}</span>
                    <span class="step-option-desc">${schema.description}</span>
                </div>
                <span class="step-option-add">+</span>
            </button>
        `;
    }

    function renderTemplateOption(key, template) {
        const templateIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/><path d="M10 9H8"/></svg>`;
        return `
            <button class="template-option" data-template="${key}">
                <span class="template-icon">${templateIcon}</span>
                <div class="template-info">
                    <span class="template-name">${escapeHtml(template.name)}</span>
                    <span class="template-desc">${escapeHtml(template.description)}</span>
                </div>
                <span class="template-steps-count">${template.steps.length} steps</span>
            </button>
        `;
    }

    function loadTemplate(templateKey) {
        const template = WORKFLOW_TEMPLATES[templateKey];
        if (!template) return;

        // Confirm if there are existing steps
        if (builderSteps.length > 0) {
            if (!confirm(`This will replace your ${builderSteps.length} existing steps with the "${template.name}" template. Continue?`)) {
                return;
            }
        }

        // Deep copy template steps
        builderSteps = template.steps.map(step => JSON.parse(JSON.stringify(step)));
        expandedStepIndex = 0;
        saveBuilderState();
        renderStepsList();
    }

    // Search filter
    if (stepSearchInput) {
        stepSearchInput.addEventListener('input', (e) => {
            renderStepOptions(e.target.value);
        });
    }

    // Open step picker button
    if (addStepBtn) {
        addStepBtn.addEventListener('click', openStepPicker);
    }

    // ============================================
    // Step Management
    // ============================================
    function addStep(action) {
        const schema = STEP_SCHEMA[action];
        if (!schema) return;

        const step = { action };

        // Initialize with default values
        schema.fields.forEach(field => {
            if (field.default !== undefined) {
                step[field.name] = field.default;
            }
        });

        builderSteps.push(step);
        expandedStepIndex = builderSteps.length - 1;
        saveBuilderState();
        renderStepsList();
    }

    function updateStep(index, field, value) {
        if (builderSteps[index]) {
            builderSteps[index][field] = value;
            saveBuilderState();
            renderStepsList();
        }
    }

    function deleteStep(index) {
        builderSteps.splice(index, 1);
        if (expandedStepIndex === index) {
            expandedStepIndex = null;
        } else if (expandedStepIndex > index) {
            expandedStepIndex--;
        }
        saveBuilderState();
        renderStepsList();
    }

    function duplicateStep(index) {
        const step = JSON.parse(JSON.stringify(builderSteps[index]));
        builderSteps.splice(index + 1, 0, step);
        expandedStepIndex = index + 1;
        saveBuilderState();
        renderStepsList();
    }

    function moveStep(fromIndex, toIndex) {
        if (toIndex < 0 || toIndex >= builderSteps.length) return;

        const step = builderSteps.splice(fromIndex, 1)[0];
        builderSteps.splice(toIndex, 0, step);

        if (expandedStepIndex === fromIndex) {
            expandedStepIndex = toIndex;
        } else if (expandedStepIndex === toIndex) {
            expandedStepIndex = fromIndex;
        }

        saveBuilderState();
        renderStepsList();
    }

    function clearAllSteps() {
        if (builderSteps.length === 0) return;

        if (confirm('Are you sure you want to clear all steps?')) {
            builderSteps = [];
            expandedStepIndex = null;
            saveBuilderState();
            renderStepsList();
        }
    }

    if (clearWorkflowBtn) {
        clearWorkflowBtn.addEventListener('click', clearAllSteps);
    }

    // ============================================
    // Validation
    // ============================================
    const VALIDATION_RULES = {
        goto: (step) => {
            const errors = {};
            if (!step.url || !step.url.trim()) {
                errors.url = 'URL is required';
            } else if (!isValidURL(step.url)) {
                errors.url = 'Invalid URL format';
            }
            return errors;
        },
        click: (step) => {
            const errors = {};
            if (!step.auto_detect && (!step.selector || !step.selector.trim())) {
                errors.selector = 'Selector required (or enable auto-detect)';
            }
            return errors;
        },
        type: (step) => {
            const errors = {};
            if (!step.selector || !step.selector.trim()) {
                errors.selector = 'Selector is required';
            }
            if (!step.value) {
                errors.value = 'Text value is required';
            }
            return errors;
        },
        wait: (step) => {
            const errors = {};
            if (step.duration === undefined || step.duration === '' || step.duration < 0) {
                errors.duration = 'Duration must be >= 0';
            }
            return errors;
        },
        extract: (step) => {
            const errors = {};
            if (!step.selector || !step.selector.trim()) {
                errors.selector = 'Selector is required';
            }
            if (step.extract_mode === 'attribute' && !step.attribute) {
                errors.attribute = 'Attribute name required';
            }
            return errors;
        },
        fill_form: (step) => {
            const errors = {};
            if (!step.auto_detect && (!step.fields || Object.keys(step.fields).length === 0)) {
                errors.fields = 'Add field mappings or enable auto-detect';
            }
            return errors;
        },
        screenshot: () => ({}),
        scroll: (step) => {
            const errors = {};
            const mode = step.scroll_mode || 'pixels';
            if (mode === 'to_element') {
                if (!step.selector || !step.selector.trim()) {
                    errors.selector = 'Target element selector is required';
                }
            } else if (mode === 'until_text') {
                if (!step.scroll_text || !step.scroll_text.trim()) {
                    errors.scroll_text = 'Search text is required';
                }
            }
            return errors;
        },
        upload: (step) => {
            const errors = {};
            if (!step.selector || !step.selector.trim()) {
                errors.selector = 'File input selector is required';
            }
            if (!step.file || !step.file.trim()) {
                errors.file = 'Filename is required';
            }
            return errors;
        }
    };

    function validateStep(step) {
        const validate = VALIDATION_RULES[step.action];
        if (!validate) return {};
        return validate(step);
    }

    function isValidURL(str) {
        try {
            new URL(str);
            return true;
        } catch {
            return false;
        }
    }

    function getStepValidationMessage(step) {
        const errors = validateStep(step);
        const errorKeys = Object.keys(errors);
        if (errorKeys.length === 0) return null;
        return errors[errorKeys[0]];
    }

    // ============================================
    // Step Card Rendering
    // ============================================
    function renderStepsList() {
        if (!stepsList) return;

        if (builderSteps.length === 0) {
            stepsList.innerHTML = '';
            if (emptySteps) {
                emptySteps.style.display = 'flex';
                stepsList.appendChild(emptySteps);
            }
            return;
        }

        if (emptySteps) {
            emptySteps.style.display = 'none';
        }

        stepsList.innerHTML = builderSteps.map((step, index) => {
            return renderStepCard(step, index);
        }).join('');

        // Add event listeners
        attachStepCardListeners();
    }

    function renderStepCard(step, index) {
        const schema = STEP_SCHEMA[step.action];
        if (!schema) return '';

        const errors = validateStep(step);
        const hasErrors = Object.keys(errors).length > 0;
        const isExpanded = expandedStepIndex === index;
        const validationMsg = hasErrors ? getStepValidationMessage(step) : null;

        return `
            <div class="step-card ${hasErrors ? 'has-errors' : ''} ${isExpanded ? 'expanded' : ''}" data-index="${index}" draggable="true">
                <div class="step-card-header" data-action="toggle">
                    <span class="step-handle" title="Drag to reorder">⋮⋮</span>
                    <span class="step-number">${index + 1}</span>
                    <span class="step-icon">${schema.icon}</span>
                    <span class="step-title">${schema.label}</span>
                    ${validationMsg ? `<span class="step-validation">${escapeHtml(validationMsg)}</span>` : ''}
                    <button class="step-menu-btn" data-action="menu" title="Step options">⋮</button>
                </div>
                <div class="step-card-body">
                    ${renderStepFields(step, index, schema, errors)}
                </div>
                <div class="step-menu" data-step="${index}">
                    <button class="step-menu-item" data-action="duplicate">
                        <span>⎘</span> Duplicate
                    </button>
                    <button class="step-menu-item" data-action="move-up" ${index === 0 ? 'disabled' : ''}>
                        <span>↑</span> Move Up
                    </button>
                    <button class="step-menu-item" data-action="move-down" ${index === builderSteps.length - 1 ? 'disabled' : ''}>
                        <span>↓</span> Move Down
                    </button>
                    <button class="step-menu-item danger" data-action="delete">
                        <span>🗑</span> Delete
                    </button>
                </div>
            </div>
        `;
    }

    function renderStepFields(step, index, schema, errors) {
        return schema.fields.map(field => {
            // Check showWhen condition
            if (field.showWhen) {
                const [condField, condValue] = Object.entries(field.showWhen)[0];
                if (step[condField] !== condValue) {
                    return '';
                }
            }

            const error = errors[field.name];
            const value = step[field.name];

            switch (field.type) {
                case 'url':
                case 'text':
                    const isSelector = field.name === 'selector' || field.label.toLowerCase().includes('selector');
                    // For scroll action's selector field, use scroll-target mode
                    const pickerModeAttr = (step.action === 'scroll' && field.name === 'selector') ? 'scroll-target' : 'click';
                    const selectBtnHtml = isSelector ? `
                        <button type="button" class="btn-select-element" data-index="${index}" data-field="${field.name}" data-picker-mode="${pickerModeAttr}" title="Select from page">
                            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4Z"/></svg>
                        </button>
                    ` : '';
                    return `
                        <div class="step-field">
                            <label class="step-field-label">${field.label}${field.required ? ' *' : ''}</label>
                            <div class="${isSelector ? 'step-field-with-button' : ''}">
                                <input
                                    type="${field.type === 'url' ? 'url' : 'text'}"
                                    class="step-field-input ${error ? 'has-error' : ''}"
                                    data-field="${field.name}"
                                    data-index="${index}"
                                    value="${escapeHtml(value || '')}"
                                    placeholder="${field.placeholder || ''}"
                                >
                                ${selectBtnHtml}
                            </div>
                            ${error ? `<div class="step-field-error">${escapeHtml(error)}</div>` : ''}
                            ${field.hint && !error ? `<div class="step-field-hint">${escapeHtml(field.hint)}</div>` : ''}
                        </div>
                    `;

                case 'number':
                    return `
                        <div class="step-field">
                            <label class="step-field-label">${field.label}${field.required ? ' *' : ''}</label>
                            <input
                                type="number"
                                class="step-field-input ${error ? 'has-error' : ''}"
                                data-field="${field.name}"
                                data-index="${index}"
                                value="${value !== undefined ? value : ''}"
                                placeholder="${field.placeholder || ''}"
                                min="${field.min !== undefined ? field.min : ''}"
                            >
                            ${error ? `<div class="step-field-error">${escapeHtml(error)}</div>` : ''}
                            ${field.hint && !error ? `<div class="step-field-hint">${escapeHtml(field.hint)}</div>` : ''}
                        </div>
                    `;

                case 'select':
                    return `
                        <div class="step-field">
                            <label class="step-field-label">${field.label}</label>
                            <select class="step-field-select" data-field="${field.name}" data-index="${index}">
                                ${field.options.map(opt => `
                                    <option value="${opt}" ${value === opt ? 'selected' : ''}>${opt}</option>
                                `).join('')}
                            </select>
                        </div>
                    `;

                case 'toggle':
                    return `
                        <div class="step-field">
                            <div class="step-field-toggle">
                                <div class="toggle-switch ${value ? 'active' : ''}" data-field="${field.name}" data-index="${index}"></div>
                                <span class="toggle-label">${field.label}</span>
                            </div>
                        </div>
                    `;

                case 'key-value':
                    const fields = value || {};
                    const entries = Object.entries(fields);
                    return `
                        <div class="step-field">
                            <label class="step-field-label">${field.label}</label>
                            <div class="key-value-fields" data-field="${field.name}" data-index="${index}">
                                ${entries.map(([k, v], i) => `
                                    <div class="key-value-row">
                                        <input type="text" class="step-field-input kv-key" value="${escapeHtml(k)}" placeholder="Field name">
                                        <input type="text" class="step-field-input kv-value" value="${escapeHtml(v)}" placeholder="Value or {{user.field}}">
                                        <button class="key-value-remove" data-kv-index="${i}">×</button>
                                    </div>
                                `).join('')}
                                <button class="key-value-add">+ Add Field</button>
                            </div>
                            ${error ? `<div class="step-field-error">${escapeHtml(error)}</div>` : ''}
                        </div>
                    `;

                default:
                    return '';
            }
        }).join('');
    }

    function attachStepCardListeners() {
        // Toggle expand/collapse
        document.querySelectorAll('.step-card-header[data-action="toggle"]').forEach(header => {
            header.addEventListener('click', (e) => {
                if (e.target.closest('.step-menu-btn')) return;

                const card = header.closest('.step-card');
                const index = parseInt(card.dataset.index);

                expandedStepIndex = expandedStepIndex === index ? null : index;
                renderStepsList();
            });
        });

        // Menu button
        document.querySelectorAll('.step-menu-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const card = btn.closest('.step-card');
                const menu = card.querySelector('.step-menu');

                // Close other menus
                document.querySelectorAll('.step-menu.visible').forEach(m => {
                    if (m !== menu) m.classList.remove('visible');
                });

                menu.classList.toggle('visible');
            });
        });

        // Menu actions
        document.querySelectorAll('.step-menu-item').forEach(item => {
            item.addEventListener('click', (e) => {
                const menu = item.closest('.step-menu');
                const index = parseInt(menu.dataset.step);
                const action = item.dataset.action;

                menu.classList.remove('visible');

                switch (action) {
                    case 'duplicate':
                        duplicateStep(index);
                        break;
                    case 'move-up':
                        moveStep(index, index - 1);
                        break;
                    case 'move-down':
                        moveStep(index, index + 1);
                        break;
                    case 'delete':
                        deleteStep(index);
                        break;
                }
            });
        });

        // Input fields
        document.querySelectorAll('.step-field-input').forEach(input => {
            input.addEventListener('change', (e) => {
                const index = parseInt(input.dataset.index);
                const field = input.dataset.field;
                let value = input.value;

                if (input.type === 'number') {
                    value = parseInt(value) || 0;
                }

                updateStep(index, field, value);
            });
        });

        // Select fields
        document.querySelectorAll('.step-field-select').forEach(select => {
            select.addEventListener('change', (e) => {
                const index = parseInt(select.dataset.index);
                const field = select.dataset.field;
                updateStep(index, field, select.value);
            });
        });

        // Select Element buttons (for element picker)
        document.querySelectorAll('.btn-select-element').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                e.preventDefault();
                const index = parseInt(btn.dataset.index);
                const field = btn.dataset.field;
                const mode = btn.dataset.pickerMode || 'click';
                openElementPicker(index, field, mode);
            });
        });

        // Toggle switches
        document.querySelectorAll('.toggle-switch').forEach(toggle => {
            toggle.addEventListener('click', (e) => {
                const index = parseInt(toggle.dataset.index);
                const field = toggle.dataset.field;
                const currentValue = builderSteps[index][field];
                updateStep(index, field, !currentValue);
            });
        });

        // Key-value add button
        document.querySelectorAll('.key-value-add').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const container = btn.closest('.key-value-fields');
                const index = parseInt(container.dataset.index);
                const field = container.dataset.field;

                const current = builderSteps[index][field] || {};
                const newKey = `field_${Object.keys(current).length + 1}`;
                current[newKey] = '';

                updateStep(index, field, current);
            });
        });

        // Key-value remove button
        document.querySelectorAll('.key-value-remove').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const container = btn.closest('.key-value-fields');
                const index = parseInt(container.dataset.index);
                const field = container.dataset.field;
                const kvIndex = parseInt(btn.dataset.kvIndex);

                const current = builderSteps[index][field] || {};
                const entries = Object.entries(current);
                entries.splice(kvIndex, 1);

                updateStep(index, field, Object.fromEntries(entries));
            });
        });

        // Key-value field changes
        document.querySelectorAll('.key-value-row').forEach(row => {
            const keyInput = row.querySelector('.kv-key');
            const valueInput = row.querySelector('.kv-value');

            const updateKV = () => {
                const container = row.closest('.key-value-fields');
                const index = parseInt(container.dataset.index);
                const field = container.dataset.field;

                const newFields = {};
                container.querySelectorAll('.key-value-row').forEach(r => {
                    const k = r.querySelector('.kv-key').value.trim();
                    const v = r.querySelector('.kv-value').value;
                    if (k) newFields[k] = v;
                });

                builderSteps[index][field] = newFields;
                saveBuilderState();
            };

            keyInput?.addEventListener('change', updateKV);
            valueInput?.addEventListener('change', updateKV);
        });

        // Close menus when clicking outside
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.step-menu') && !e.target.closest('.step-menu-btn')) {
                document.querySelectorAll('.step-menu.visible').forEach(m => {
                    m.classList.remove('visible');
                });
            }
        });

        // Drag and drop handlers
        document.querySelectorAll('.step-card').forEach(card => {
            card.addEventListener('dragstart', (e) => {
                draggedStepIndex = parseInt(card.dataset.index);
                card.classList.add('dragging');
                e.dataTransfer.effectAllowed = 'move';
                e.dataTransfer.setData('text/plain', draggedStepIndex.toString());
            });

            card.addEventListener('dragend', () => {
                card.classList.remove('dragging');
                draggedStepIndex = null;
                // Remove all drag-over classes
                document.querySelectorAll('.step-card.drag-over').forEach(c => {
                    c.classList.remove('drag-over');
                });
            });

            card.addEventListener('dragover', (e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                const targetIndex = parseInt(card.dataset.index);
                if (draggedStepIndex !== null && draggedStepIndex !== targetIndex) {
                    card.classList.add('drag-over');
                }
            });

            card.addEventListener('dragleave', () => {
                card.classList.remove('drag-over');
            });

            card.addEventListener('drop', (e) => {
                e.preventDefault();
                card.classList.remove('drag-over');
                const targetIndex = parseInt(card.dataset.index);
                if (draggedStepIndex !== null && draggedStepIndex !== targetIndex) {
                    moveStep(draggedStepIndex, targetIndex);
                }
                draggedStepIndex = null;
            });
        });
    }

    // ============================================
    // JSON Generation & Preview
    // ============================================
    function generateWorkflowJSON() {
        return builderSteps.map(step => {
            const schema = STEP_SCHEMA[step.action];
            if (!schema) return null;

            const json = { action: step.action };

            schema.fields.forEach(field => {
                const value = step[field.name];
                if (value !== undefined && value !== '' && value !== null) {
                    // Skip showWhen fields if condition not met
                    if (field.showWhen) {
                        const [condField, condValue] = Object.entries(field.showWhen)[0];
                        if (step[condField] !== condValue) return;
                    }
                    json[field.name] = value;
                }
            });

            return json;
        }).filter(Boolean);
    }

    function openJsonPreview() {
        const json = generateWorkflowJSON();
        jsonPreviewContent.textContent = JSON.stringify(json, null, 2);
        jsonPreviewModal.classList.remove('hidden');
    }

    window.closeJsonPreview = function() {
        jsonPreviewModal.classList.add('hidden');
    };

    window.copyJsonToClipboard = async function() {
        const json = generateWorkflowJSON();
        try {
            await navigator.clipboard.writeText(JSON.stringify(json, null, 2));
            const btn = document.getElementById('copy-json-btn');
            const originalText = btn.innerHTML;
            btn.innerHTML = '✓ Copied!';
            setTimeout(() => {
                btn.innerHTML = originalText;
            }, 2000);
        } catch (err) {
            showError('Failed to copy to clipboard');
        }
    };

    if (previewJsonBtn) {
        previewJsonBtn.addEventListener('click', openJsonPreview);
    }

    // ============================================
    // Run Builder Workflow
    // ============================================
    function collectBuilderUserData() {
        const userData = {};
        if (builderFirstName && builderFirstName.value.trim()) {
            userData.first_name = builderFirstName.value.trim();
        }
        if (builderLastName && builderLastName.value.trim()) {
            userData.last_name = builderLastName.value.trim();
        }
        if (builderEmail && builderEmail.value.trim()) {
            userData.email = builderEmail.value.trim();
        }
        if (builderPhone && builderPhone.value.trim()) {
            userData.phone = builderPhone.value.trim();
        }
        if (builderLocation && builderLocation.value.trim()) {
            userData.location = builderLocation.value.trim();
        }
        if (builderLinkedin && builderLinkedin.value.trim()) {
            userData.linkedin_url = builderLinkedin.value.trim();
        }
        return userData;
    }

    async function runBuilderWorkflow() {
        if (builderSteps.length === 0) {
            showError('Please add at least one step to the workflow');
            return;
        }

        // Validate all steps
        let hasErrors = false;
        builderSteps.forEach((step, index) => {
            const errors = validateStep(step);
            if (Object.keys(errors).length > 0) {
                hasErrors = true;
                expandedStepIndex = index;
            }
        });

        if (hasErrors) {
            renderStepsList();
            showError('Please fix validation errors before running');
            return;
        }

        const steps = generateWorkflowJSON();
        const userData = collectBuilderUserData();

        setWorkflowState(WorkflowState.RUNNING);
        clearTimeline();
        hideError();
        showTimelineLoading();

        // Disable builder controls
        if (runBuilderBtn) runBuilderBtn.disabled = true;
        if (addStepBtn) addStepBtn.disabled = true;
        if (clearWorkflowBtn) clearWorkflowBtn.disabled = true;

        try {
            const response = await fetch('/api/workflow/execute-steps', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ steps, user_data: userData })
            });

            removeTimelineLoading();

            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
                throw new Error(errorData.detail || `HTTP ${response.status}`);
            }

            const data = await response.json();

            // Render parsed workflow (what we sent)
            renderWorkflowParsed(steps);

            // Render each step result
            (data.steps || []).forEach((step, index) => {
                renderStepExecution(step, index);
                renderScreenshot(step, index);
            });

            // Render final status
            renderFinalStatus(data);

            setWorkflowState(data.success ? WorkflowState.DONE : WorkflowState.FAILED);

        } catch (error) {
            removeTimelineLoading();
            showError(error.message || 'Workflow execution failed');
            setWorkflowState(WorkflowState.FAILED);
        } finally {
            // Re-enable builder controls
            if (runBuilderBtn) runBuilderBtn.disabled = false;
            if (addStepBtn) addStepBtn.disabled = false;
            if (clearWorkflowBtn) clearWorkflowBtn.disabled = false;
        }
    }

    if (runBuilderBtn) {
        runBuilderBtn.addEventListener('click', runBuilderWorkflow);
    }

    // ============================================
    // LocalStorage Persistence
    // ============================================
    function saveBuilderState() {
        try {
            localStorage.setItem('builderSteps', JSON.stringify(builderSteps));
        } catch (e) {
            console.warn('Failed to save builder state:', e);
        }
    }

    function loadBuilderState() {
        try {
            const saved = localStorage.getItem('builderSteps');
            if (saved) {
                builderSteps = JSON.parse(saved);
            }
        } catch (e) {
            console.warn('Failed to load builder state:', e);
            builderSteps = [];
        }
    }

    // ============================================
    // Builder Initialization
    // ============================================
    loadBuilderState();
    renderStepOptions();

    // If there are saved steps, show builder mode indicator
    if (builderSteps.length > 0) {
        const builderTab = document.querySelector('.mode-tab[data-mode="builder"]');
        if (builderTab) {
            builderTab.style.position = 'relative';
            const badge = document.createElement('span');
            badge.className = 'step-badge';
            badge.style.cssText = 'position: absolute; top: -4px; right: -4px; width: 16px; height: 16px; font-size: 0.625rem;';
            badge.textContent = builderSteps.length;
            builderTab.appendChild(badge);
        }
    }

    // ============================================
    // ELEMENT PICKER
    // ============================================

    let pickerTargetField = null;  // Which field to populate when element is selected
    let pickerTargetIndex = null;  // Step index for the field
    let pickerMode = 'click';  // 'click' (default - closes on select) | 'scroll-target' (stays open for interaction)

    const pickerModal = document.getElementById('element-picker-modal');
    const pickerHint = document.getElementById('picker-hint');
    const pickerDoneBtn = document.getElementById('picker-done-btn');
    const pickerUrlInput = document.getElementById('picker-url');
    const pickerLoadBtn = document.getElementById('picker-load-btn');
    const pickerViewport = document.getElementById('picker-viewport');
    const pickerScreenshot = document.getElementById('picker-screenshot');
    const pickerOverlays = document.getElementById('picker-overlays');
    const pickerLoading = document.getElementById('picker-loading');
    const pickerEmpty = document.getElementById('picker-empty');
    const pickerElementCount = document.getElementById('picker-element-count');
    const pickerScrollControls = document.getElementById('picker-scroll-controls');
    const pickerScrollUp = document.getElementById('picker-scroll-up');
    const pickerScrollDown = document.getElementById('picker-scroll-down');
    let currentPickerViewport = { width: 1280, height: 720 };

    window.openElementPicker = function(stepIndex, fieldName, mode = 'click') {
        pickerTargetIndex = stepIndex;
        pickerTargetField = fieldName;
        pickerMode = mode;

        // Reset state
        if (pickerScreenshot) pickerScreenshot.classList.add('hidden');
        if (pickerOverlays) pickerOverlays.innerHTML = '';
        if (pickerEmpty) pickerEmpty.classList.remove('hidden');
        if (pickerLoading) pickerLoading.classList.add('hidden');
        if (pickerElementCount) pickerElementCount.textContent = '';
        if (pickerScrollControls) pickerScrollControls.classList.add('hidden');

        // Update UI based on mode
        if (pickerHint) {
            if (mode === 'scroll-target') {
                pickerHint.textContent = 'Click element to set as scroll target';
            } else {
                pickerHint.textContent = 'Click an element to select it';
            }
        }
        if (pickerDoneBtn) {
            if (mode === 'scroll-target') {
                pickerDoneBtn.classList.remove('hidden');
            } else {
                pickerDoneBtn.classList.add('hidden');
            }
        }

        // Pre-fill URL from goto step if available
        const gotoStep = builderSteps.find(s => s.action === 'goto' && s.url);
        if (gotoStep && pickerUrlInput) {
            pickerUrlInput.value = gotoStep.url;
        }

        if (pickerModal) {
            pickerModal.classList.remove('hidden');
            if (pickerUrlInput) pickerUrlInput.focus();
        }
    };

    window.closeElementPicker = function() {
        if (pickerModal) pickerModal.classList.add('hidden');
        pickerTargetField = null;
        pickerTargetIndex = null;
        pickerMode = 'click';
        // Reset hint text and done button
        if (pickerHint) pickerHint.textContent = 'Click an element to select it';
        if (pickerDoneBtn) pickerDoneBtn.classList.add('hidden');
    };

    async function loadPageForPicker() {
        const url = pickerUrlInput ? pickerUrlInput.value.trim() : '';
        if (!url) {
            showError('Please enter a URL');
            return;
        }

        // Show loading
        if (pickerEmpty) pickerEmpty.classList.add('hidden');
        if (pickerScreenshot) pickerScreenshot.classList.add('hidden');
        if (pickerOverlays) pickerOverlays.innerHTML = '';
        if (pickerLoading) pickerLoading.classList.remove('hidden');
        if (pickerElementCount) pickerElementCount.textContent = 'Loading...';

        try {
            const response = await fetch('/api/element-picker/load', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url })
            });

            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.detail || 'Failed to load page');
            }

            const data = await response.json();

            // Store viewport for scroll operations
            currentPickerViewport = data.viewport || { width: 1280, height: 720 };

            // Hide loading
            if (pickerLoading) pickerLoading.classList.add('hidden');

            // Display screenshot
            if (pickerScreenshot) {
                pickerScreenshot.src = 'data:image/jpeg;base64,' + data.screenshot_base64;
                pickerScreenshot.classList.remove('hidden');

                // Wait for image to load to get dimensions
                pickerScreenshot.onload = () => {
                    renderPickerOverlays(data.elements, currentPickerViewport);
                };
            }

            // Update element count
            if (pickerElementCount) {
                pickerElementCount.textContent = `${data.element_count} elements found`;
            }

            // Show scroll controls
            if (pickerScrollControls) {
                pickerScrollControls.classList.remove('hidden');
            }

        } catch (error) {
            if (pickerLoading) pickerLoading.classList.add('hidden');
            if (pickerEmpty) pickerEmpty.classList.remove('hidden');
            if (pickerElementCount) pickerElementCount.textContent = '';
            showError(error.message);
        }
    }

    function renderPickerOverlays(elements, viewport) {
        if (!pickerOverlays || !pickerScreenshot) return;

        pickerOverlays.innerHTML = '';

        // Get the actual displayed position of the screenshot
        const imgRect = pickerScreenshot.getBoundingClientRect();
        const viewportRect = pickerViewport.getBoundingClientRect();

        // Calculate scale factor (screenshot may be displayed smaller than viewport)
        const scaleX = imgRect.width / viewport.width;
        const scaleY = imgRect.height / viewport.height;

        // Position overlays container to match screenshot position within viewport
        const offsetX = imgRect.left - viewportRect.left + pickerViewport.scrollLeft;
        const offsetY = imgRect.top - viewportRect.top + pickerViewport.scrollTop;

        pickerOverlays.style.width = imgRect.width + 'px';
        pickerOverlays.style.height = imgRect.height + 'px';
        pickerOverlays.style.left = offsetX + 'px';
        pickerOverlays.style.top = offsetY + 'px';

        elements.forEach(el => {
            const box = document.createElement('div');
            box.className = 'picker-overlay-box';
            box.style.left = (el.bbox.x * scaleX) + 'px';
            box.style.top = (el.bbox.y * scaleY) + 'px';
            box.style.width = (el.bbox.width * scaleX) + 'px';
            box.style.height = (el.bbox.height * scaleY) + 'px';
            box.dataset.selector = el.selector;
            box.title = el.text || el.tag;

            box.addEventListener('click', () => {
                selectPickerElement(el.selector);
            });

            pickerOverlays.appendChild(box);
        });
    }

    function selectPickerElement(selector) {
        if (pickerTargetIndex !== null && pickerTargetField) {
            // Update the step with selected selector
            updateStep(pickerTargetIndex, pickerTargetField, selector);
        }

        // Handle based on picker mode
        if (pickerMode === 'click') {
            // Default: close immediately after selection
            closeElementPicker();
        } else if (pickerMode === 'scroll-target') {
            // Scroll-target mode: show confirmation but keep picker open
            if (pickerHint) {
                pickerHint.innerHTML = `Selected: <code>${escapeHtml(selector)}</code> - Click "Done" to close or pick another`;
            }
        }
    }

    // Scroll the picker browser page
    async function scrollPickerPage(direction) {
        // Disable buttons during scroll
        if (pickerScrollUp) pickerScrollUp.disabled = true;
        if (pickerScrollDown) pickerScrollDown.disabled = true;
        if (pickerElementCount) pickerElementCount.textContent = 'Scrolling...';

        try {
            const response = await fetch('/api/element-picker/scroll', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ direction, amount: 500 })
            });

            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.detail || 'Scroll failed');
            }

            const data = await response.json();

            // Update screenshot
            if (pickerScreenshot) {
                pickerScreenshot.src = 'data:image/jpeg;base64,' + data.screenshot_base64;
                // Wait for image to load then update overlays
                pickerScreenshot.onload = () => {
                    renderPickerOverlays(data.elements, currentPickerViewport);
                };
            }

            // Update element count
            if (pickerElementCount) {
                pickerElementCount.textContent = `${data.element_count} elements found`;
            }

        } catch (error) {
            showError(error.message);
            if (pickerElementCount) pickerElementCount.textContent = 'Scroll failed';
        } finally {
            // Re-enable buttons
            if (pickerScrollUp) pickerScrollUp.disabled = false;
            if (pickerScrollDown) pickerScrollDown.disabled = false;
        }
    }

    // Event listeners for element picker
    if (pickerLoadBtn) {
        pickerLoadBtn.addEventListener('click', loadPageForPicker);
    }

    if (pickerUrlInput) {
        pickerUrlInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                loadPageForPicker();
            }
        });
    }

    // Scroll button event listeners
    if (pickerScrollUp) {
        pickerScrollUp.addEventListener('click', () => scrollPickerPage('up'));
    }

    if (pickerScrollDown) {
        pickerScrollDown.addEventListener('click', () => scrollPickerPage('down'));
    }

    // Close picker on escape
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && pickerModal && !pickerModal.classList.contains('hidden')) {
            closeElementPicker();
        }
    });
});
