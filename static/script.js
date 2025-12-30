document.addEventListener('DOMContentLoaded', function() {
    const fileInput = document.getElementById('file-input');
    const uploadForm = document.getElementById('upload-form');
    const clearButton = document.getElementById('clear-button');
    const dropZone = document.getElementById('drop-zone');

    if (fileInput && clearButton && dropZone) {
        const dropZoneText = dropZone.querySelector('span');
        const originalDropZoneText = dropZoneText.innerHTML;

        // Show clear button and file name on file selection
        fileInput.addEventListener('change', function() {
            if (this.files && this.files.length > 0) {
                if (this.files.length === 1) {
                    dropZoneText.textContent = this.files[0].name;
                } else {
                    dropZoneText.textContent = `${this.files.length} files selected`;
                }
                clearButton.hidden = false;
            } else {
                dropZoneText.innerHTML = originalDropZoneText;
                clearButton.hidden = true;
            }
        });

        // Handle clear button click
        clearButton.addEventListener('click', function() {
            uploadForm.reset();
            fileInput.dispatchEvent(new Event('change', { 'bubbles': true }));
        });
    }

    // --- Scheduler Interval Control ---
    const schedulerForm = document.getElementById('scheduler-form');
    if (schedulerForm) {
        const intervalInput = document.getElementById('scheduler-interval');
        const updateButton = schedulerForm.querySelector('button');

        schedulerForm.addEventListener('submit', function(event) {
            event.preventDefault();
            const interval = intervalInput.value;

            if (!interval || interval <= 0) {
                showFlashMessage('Please enter a valid interval.', 'warning');
                return;
            }

            updateButton.disabled = true;
            updateButton.textContent = 'Updating...';

            fetch('/api/scheduler/interval', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    // If you use CSRF tokens, you need to include them here.
                },
                body: JSON.stringify({ interval: interval })
            })
            .then(response => {
                if (!response.ok) {
                    // This helps debug the JSON error you mentioned.
                    // We read the response as text first.
                    return response.text().then(text => {
                        throw new Error(`Server error: ${response.status} ${response.statusText}. Check console for response.`);
                    });
                }
                return response.json();
            })
            .then(data => {
                if (data.status === 'success') {
                    showFlashMessage(data.message || 'Scheduler interval updated successfully!', 'success');
                } else {
                    throw new Error(data.message || 'Failed to update interval.');
                }
            })
            .catch(error => {
                showFlashMessage(error.message, 'danger');
                console.error('Error updating scheduler interval:', error);
            })
            .finally(() => {
                updateButton.disabled = false;
                updateButton.textContent = 'Update';
            });
        });
    }

    // --- Flash Message Helper ---
    function showFlashMessage(message, category = 'info') {
        const container = document.querySelector('.flash-container');
        if (!container) {
            console.error('".flash-container" not found. Cannot display flash message.');
            alert(`${category.toUpperCase()}: ${message}`); // Fallback to a simple alert
            return;
        }

        const flash = document.createElement('div');
        flash.className = `flash ${category}`;
        flash.innerHTML = `<span>${message}</span><button class="flash-close">&times;</button>`;
        
        container.appendChild(flash);

        const removeFlash = () => {
            flash.style.animation = 'fadeOutFlash 0.5s forwards';
            setTimeout(() => flash.remove(), 500);
        };

        flash.querySelector('.flash-close').addEventListener('click', removeFlash);
        setTimeout(removeFlash, 4500); // Auto-dismiss after 4.5 seconds
    }
});
