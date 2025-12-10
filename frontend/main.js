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
});
