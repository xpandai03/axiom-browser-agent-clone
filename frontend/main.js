document.addEventListener('DOMContentLoaded', function() {
    const runBtn = document.getElementById('run-btn');
    const instructionsInput = document.getElementById('instructions');
    const jobDescInput = document.getElementById('job-description');
    const resumeInput = document.getElementById('resume');
    const outputSection = document.getElementById('output-section');
    const workflowStepsDiv = document.getElementById('workflow-steps');
    const executionLogsDiv = document.getElementById('execution-logs');
    const tailoredResumeDiv = document.getElementById('tailored-resume');
    const resumePanel = document.getElementById('resume-panel');
    const loadingDiv = document.getElementById('loading');
    const errorDiv = document.getElementById('error');

    runBtn.addEventListener('click', async function() {
        const instructions = instructionsInput.value.trim();
        
        if (!instructions) {
            showError('Please enter workflow instructions');
            return;
        }

        hideError();
        showLoading();
        hideOutput();

        const formData = new FormData();
        formData.append('instructions', instructions);
        formData.append('job_description', jobDescInput.value.trim());
        
        if (resumeInput.files.length > 0) {
            formData.append('resume', resumeInput.files[0]);
        }

        try {
            const response = await fetch('/run-workflow', {
                method: 'POST',
                body: formData
            });

            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }

            const data = await response.json();
            hideLoading();
            displayResults(data);
        } catch (error) {
            hideLoading();
            showError('Error running workflow: ' + error.message);
        }
    });

    function displayResults(data) {
        workflowStepsDiv.textContent = JSON.stringify(data.workflow_steps, null, 2);

        executionLogsDiv.innerHTML = '';
        data.execution_logs.forEach(log => {
            const logEntry = document.createElement('div');
            logEntry.className = 'log-entry';
            logEntry.innerHTML = `
                <span class="log-step">Step ${log.step}</span>
                <span class="log-action">${log.action}</span>
                <span class="log-status ${log.status}">${log.status}</span>
                <span class="log-details">${log.details || log.message || ''}</span>
            `;
            executionLogsDiv.appendChild(logEntry);
        });

        if (data.tailored_resume) {
            resumePanel.style.display = 'block';
            tailoredResumeDiv.innerHTML = formatMarkdown(data.tailored_resume);
        } else {
            resumePanel.style.display = 'none';
        }

        outputSection.style.display = 'grid';
    }

    function formatMarkdown(text) {
        return text
            .replace(/## (.*)/g, '<h2>$1</h2>')
            .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
            .replace(/\n- (.*)/g, '<li>$1</li>')
            .replace(/(<li>.*<\/li>)+/g, '<ul>$&</ul>')
            .replace(/\n/g, '<br>');
    }

    function showLoading() {
        loadingDiv.style.display = 'block';
        runBtn.disabled = true;
    }

    function hideLoading() {
        loadingDiv.style.display = 'none';
        runBtn.disabled = false;
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
