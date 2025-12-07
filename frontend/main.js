document.addEventListener('DOMContentLoaded', function() {
    // DOM Elements
    const runBtn = document.getElementById('run-btn');
    const testBtn = document.getElementById('test-btn');
    const instructionsInput = document.getElementById('instructions');
    const jobDescInput = document.getElementById('job-description');
    const resumeInput = document.getElementById('resume');
    const userNameInput = document.getElementById('user-name');
    const userEmailInput = document.getElementById('user-email');
    const userPhoneInput = document.getElementById('user-phone');
    const outputSection = document.getElementById('output-section');
    const workflowStepsDiv = document.getElementById('workflow-steps');
    const executionStatusDiv = document.getElementById('execution-status');
    const executionLogsDiv = document.getElementById('execution-logs');
    const tailoredResumeDiv = document.getElementById('tailored-resume');
    const resumePanel = document.getElementById('resume-panel');
    const screenshotPanel = document.getElementById('screenshot-panel');
    const screenshotGallery = document.getElementById('screenshot-gallery');
    const screenshotViewer = document.getElementById('screenshot-viewer');
    const viewerImage = document.getElementById('viewer-image');
    const viewerCaption = document.getElementById('viewer-caption');
    const closeViewerBtn = document.getElementById('close-viewer');
    const loadingDiv = document.getElementById('loading');
    const loadingText = document.getElementById('loading-text');
    const errorDiv = document.getElementById('error');

    // Test workflow instructions
    const TEST_WORKFLOW = `Go to https://example.com
Wait 2 seconds
Take a screenshot`;

    // Event Listeners
    runBtn.addEventListener('click', runWorkflow);
    testBtn.addEventListener('click', runTestWorkflow);
    closeViewerBtn.addEventListener('click', closeScreenshotViewer);

    // Close viewer on escape key
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeScreenshotViewer();
    });

    function runTestWorkflow() {
        instructionsInput.value = TEST_WORKFLOW;
        runWorkflow();
    }

    async function runWorkflow() {
        const instructions = instructionsInput.value.trim();

        if (!instructions) {
            showError('Please enter workflow instructions');
            return;
        }

        hideError();
        showLoading('Parsing workflow instructions...');
        hideOutput();

        // Collect user data
        const userData = {};
        if (userNameInput.value.trim()) userData.name = userNameInput.value.trim();
        if (userEmailInput.value.trim()) userData.email = userEmailInput.value.trim();
        if (userPhoneInput.value.trim()) userData.phone = userPhoneInput.value.trim();

        // Build form data
        const formData = new FormData();
        formData.append('instructions', instructions);
        formData.append('job_description', jobDescInput.value.trim());
        formData.append('user_data', JSON.stringify(userData));

        if (resumeInput.files.length > 0) {
            formData.append('resume', resumeInput.files[0]);
        }

        try {
            showLoading('Executing workflow via MCP...');

            // Call the API
            const response = await fetch('/api/workflow/run-sync', {
                method: 'POST',
                body: formData
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.detail || data.error || `HTTP error ${response.status}`);
            }

            hideLoading();
            displayResults(data);

        } catch (error) {
            hideLoading();
            console.error('Workflow error:', error);
            showError('Error running workflow: ' + error.message);
        }
    }

    function displayResults(data) {
        console.log('API Response:', data);

        // Display parsed workflow steps
        const workflowSteps = data.workflow_steps || [];
        workflowStepsDiv.textContent = JSON.stringify(workflowSteps, null, 2);

        // Display execution status
        const success = data.success !== false;
        const totalDuration = data.total_duration_ms || 0;
        executionStatusDiv.innerHTML = `
            <div class="status-badge ${success ? 'success' : 'failed'}">
                ${success ? 'SUCCESS' : 'FAILED'}
            </div>
            <span class="status-info">
                Workflow ID: ${data.workflow_id || 'N/A'} |
                Duration: ${totalDuration}ms |
                Steps: ${(data.steps || []).length}
            </span>
        `;

        // Display execution logs
        executionLogsDiv.innerHTML = '';
        const steps = data.steps || [];

        if (steps.length === 0) {
            executionLogsDiv.innerHTML = '<div class="no-logs">No execution logs available</div>';
        } else {
            steps.forEach((step, index) => {
                const logEntry = document.createElement('div');
                logEntry.className = `log-entry ${step.status || 'unknown'}`;

                const stepNum = step.step_number !== undefined ? step.step_number : index;
                const action = step.action || 'unknown';
                const status = step.status || 'unknown';
                const durationMs = step.duration_ms || 0;
                const logs = step.logs || [];
                const error = step.error || '';

                let detailsHtml = '';
                if (logs.length > 0) {
                    detailsHtml = logs.map(log => `<div class="log-detail">${escapeHtml(log)}</div>`).join('');
                }
                if (error) {
                    detailsHtml += `<div class="log-error">${escapeHtml(error)}</div>`;
                }

                logEntry.innerHTML = `
                    <div class="log-header">
                        <span class="log-step">Step ${stepNum}</span>
                        <span class="log-action">${escapeHtml(action)}</span>
                        <span class="log-status ${status}">${status.toUpperCase()}</span>
                        <span class="log-duration">${durationMs}ms</span>
                    </div>
                    ${detailsHtml ? `<div class="log-details">${detailsHtml}</div>` : ''}
                `;
                executionLogsDiv.appendChild(logEntry);
            });
        }

        // Display screenshots
        displayScreenshots(steps);

        // Display tailored resume if available
        if (data.tailored_resume) {
            resumePanel.style.display = 'block';
            tailoredResumeDiv.innerHTML = formatMarkdown(data.tailored_resume);
        } else {
            resumePanel.style.display = 'none';
        }

        // Display any top-level error
        if (data.error) {
            showError('Workflow error: ' + data.error);
        }

        outputSection.style.display = 'grid';
    }

    function displayScreenshots(steps) {
        screenshotGallery.innerHTML = '';
        let hasScreenshots = false;

        steps.forEach((step, index) => {
            const screenshot = step.screenshot_base64;
            if (screenshot && screenshot.length > 100) { // Valid base64 check
                hasScreenshots = true;

                const thumb = document.createElement('div');
                thumb.className = 'screenshot-thumb';

                // Create image with proper data URL
                const imgSrc = screenshot.startsWith('data:')
                    ? screenshot
                    : `data:image/jpeg;base64,${screenshot}`;

                thumb.innerHTML = `
                    <img src="${imgSrc}" alt="Step ${index} screenshot" loading="lazy">
                    <span class="thumb-label">Step ${step.step_number !== undefined ? step.step_number : index}: ${step.action || 'unknown'}</span>
                    <span class="thumb-status ${step.status || ''}">${(step.status || '').toUpperCase()}</span>
                `;

                thumb.addEventListener('click', () => {
                    openScreenshotViewer(imgSrc, `Step ${step.step_number !== undefined ? step.step_number : index}: ${step.action || 'unknown'}`);
                });

                screenshotGallery.appendChild(thumb);
            }
        });

        if (hasScreenshots) {
            screenshotPanel.style.display = 'block';
        } else {
            screenshotPanel.style.display = 'none';
            // Show message if no screenshots
            const noScreenshots = document.createElement('div');
            noScreenshots.className = 'no-screenshots';
            noScreenshots.textContent = 'No screenshots captured';
        }
    }

    function openScreenshotViewer(src, caption) {
        viewerImage.src = src;
        viewerCaption.textContent = caption;
        screenshotViewer.style.display = 'flex';
        document.body.style.overflow = 'hidden';
    }

    function closeScreenshotViewer() {
        screenshotViewer.style.display = 'none';
        document.body.style.overflow = '';
    }

    function formatMarkdown(text) {
        if (!text) return '';
        return text
            .replace(/## (.*)/g, '<h2>$1</h2>')
            .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
            .replace(/\n- (.*)/g, '<li>$1</li>')
            .replace(/(<li>.*<\/li>)+/g, '<ul>$&</ul>')
            .replace(/\n/g, '<br>');
    }

    function escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    function showLoading(message) {
        loadingText.textContent = message || 'Processing...';
        loadingDiv.style.display = 'block';
        runBtn.disabled = true;
        testBtn.disabled = true;
    }

    function hideLoading() {
        loadingDiv.style.display = 'none';
        runBtn.disabled = false;
        testBtn.disabled = false;
    }

    function showError(message) {
        errorDiv.textContent = message;
        errorDiv.style.display = 'block';
    }

    function hideError() {
        errorDiv.style.display = 'none';
    }

    function hideOutput() {
        outputSection.style.display = 'none';
    }
});
