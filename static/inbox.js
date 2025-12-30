document.addEventListener('DOMContentLoaded', () => {
    const inboxList = document.getElementById('inbox-list');
    const paginationContainer = document.getElementById('inbox-pagination');
    const conversationView = document.getElementById('conversation-view');

    // Reading Pane Elements
    const readingPane = document.getElementById('reading-pane');
    const conversationPlaceholder = document.getElementById('conversation-placeholder');
    const conversationWrapper = document.getElementById('conversation-content-wrapper');
    const conversationContent = document.getElementById('conversation-content-wrapper');
    const replyBox = document.getElementById('reply-box');
    const readingSubject = document.getElementById('reading-subject');
    const readingAvatar = document.getElementById('reading-avatar');
    const readingSenderName = document.getElementById('reading-sender-name');
    const readingTo = document.getElementById('reading-to');
    const readingTime = document.getElementById('reading-time');

    // Actions
    const replyBtn = document.getElementById('reply-btn');
    const archiveBtn = document.getElementById('archive-btn');
    const deleteBtn = document.getElementById('delete-btn');
    const cancelReplyBtn = document.getElementById('cancel-reply-btn');
    const sendReplyBtn = document.getElementById('send-reply-btn');
    const refreshBtn = document.getElementById('refresh-inbox-btn');

    // Folder Nav
    const sidebar = document.querySelector('.outlook-sidebar');
    const folderNav = document.querySelector('.folder-nav'); // Fallback if sidebar not found

    // Compose
    const composeBtnMain = document.getElementById('btn-compose-main');
    const composeModal = document.getElementById('compose-modal');
    const closeComposeBtn = document.getElementById('close-compose-btn');
    const discardComposeBtn = document.getElementById('discard-compose-btn');
    const sendComposeBtn = document.getElementById('send-compose-btn');
    const composeToInput = document.getElementById('compose-to');
    const composeSubjectInput = document.getElementById('compose-subject');
    const composeBodyInput = document.getElementById('compose-body');
    const composeReadReceipt = document.getElementById('compose-read-receipt');

    let currentFolder = 'inbox';
    let currentPage = 1;
    let activeConversationId = null;
    let activeMessageId = null;

    // --- Real API Functions ---
    const api = {
        getMail: async (folder, page = 1, search = '') => {
            const params = new URLSearchParams({ folder: folder, page, search });
            const response = await fetch(`/api/inbox/mail?${params.toString()}`, { headers: { 'X-Requested-With': 'XMLHttpRequest' } });
            if (response.redirected) { window.location.reload(); return; }
            if (!response.ok) throw new Error((await response.json()).error || 'Failed to fetch mail.');
            return response.json();
        },
        getConversation: async (conversationId) => {
            const response = await fetch(`/api/inbox/conversation/${conversationId}`, { headers: { 'X-Requested-With': 'XMLHttpRequest' } });
            if (!response.ok) throw new Error((await response.json()).error || 'Failed to fetch conversation.');
            return response.json();
        },
        sendReply: async (conversationId, body) => {
            const response = await fetch(`/api/inbox/reply/${conversationId}`, {
                method: 'POST',
                headers: { 'X-Requested-With': 'XMLHttpRequest', 'Content-Type': 'application/json' },
                body: JSON.stringify({ body }),
            });
            if (!response.ok) throw new Error((await response.json()).error || 'Reply failed.');
            return response.json();
        },
        sendMail: async (to, subject, body, readReceipt) => {
            // 'to' should be an array or comma-separated string
            const toList = to.split(',').map(e => e.trim()).filter(e => e);
            const response = await fetch(`/api/inbox/send`, {
                method: 'POST',
                headers: { 'X-Requested-With': 'XMLHttpRequest', 'Content-Type': 'application/json' },
                body: JSON.stringify({ to: toList, subject, body, read_receipt: readReceipt }),
            });
            if (!response.ok) throw new Error((await response.json()).error || 'Send failed.');
            return response.json();
        },
        markRead: async (messageId) => {
            fetch(`/api/inbox/message/${messageId}/read`, { method: 'POST' });
        },
        moveMessage: async (messageId, destination) => {
            const response = await fetch(`/api/inbox/move`, {
                method: 'POST',
                headers: { 'X-Requested-With': 'XMLHttpRequest', 'Content-Type': 'application/json' },
                body: JSON.stringify({ message_id: messageId, destination }),
            });
            if (!response.ok) throw new Error('Failed to move message');
            return response.json();
        }
    };

    function showFlashMessage(msg, type) {
        // Basic fallback
        console.log(`[${type}] ${msg}`);
        // If there's a flash container, use it
        // ...
    }

    // --- Rendering Functions ---
    function renderMailList(data) {
        inboxList.innerHTML = '';
        if (!data.items || data.items.length === 0) {
            inboxList.innerHTML = `<div style="padding: 20px; text-align: center; color: #666;">No messages found.</div>`;
            return;
        }

        data.items.forEach(item => {
            const el = document.createElement('div');
            el.className = `mail-item ${item.unread ? 'unread' : ''}`;
            el.dataset.conversationId = item.conversationId;
            el.dataset.id = item.id;

            // Outlook style items
            el.innerHTML = `
                <div class="item-top-row">
                    <span class="item-sender">${item.sender}</span>
                    <span class="item-date">${item.timestamp}</span>
                </div>
                <div class="item-subject">${item.subject || '(No Subject)'}</div>
                <div class="item-preview">${item.preview}</div>
            `;
            inboxList.appendChild(el);
        });
        feather.replace();
    }

    function renderPagination(data) {
        paginationContainer.innerHTML = '';
        if (data.total_pages <= 1) return;

        let html = '<ul class="pagination" style="justify-content: center; margin: 0;">';
        html += `<li class="page-item ${!data.has_prev ? 'disabled' : ''}"><a class="page-link" href="#" data-page="${data.current_page - 1}">&lt;</a></li>`;
        html += `<li class="page-item active"><a class="page-link" href="#">${data.current_page} / ${data.total_pages}</a></li>`;
        html += `<li class="page-item ${!data.has_next ? 'disabled' : ''}"><a class="page-link" href="#" data-page="${data.current_page + 1}">&gt;</a></li>`;
        html += '</ul>';
        paginationContainer.innerHTML = html;
    }

    function renderConversation(data) {
        activeMessageId = data.messages[data.messages.length - 1].id;

        // Populate Header
        readingSubject.textContent = data.subject || '(No Subject)';
        readingSenderName.textContent = data.participant.name;
        readingTo.textContent = 'You';
        readingTime.textContent = data.messages[data.messages.length - 1].timestamp;

        readingAvatar.style.backgroundColor = data.participant.avatar_color || '#6264a7';
        readingAvatar.textContent = data.participant.avatar_initial || 'U';

        // Render Messages
        let html = '';
        data.messages.forEach(msg => {
            html += `
                <div class="message-block">
                    <div class="message-meta">
                        <span class="message-sender-name ${msg.from_user ? 'self' : ''}">
                            ${msg.sender_name}
                        </span>
                        <span class="message-timestamp">${msg.timestamp}</span>
                    </div>
                    <div class="message-body">${msg.body}</div>
                    <hr class="message-separator">
                </div>
            `;
        });
        conversationView.innerHTML = html;

        document.getElementById('reply-to-name').textContent = data.participant.name;
        // Reply Avatar
        const replyAvatar = document.getElementById('reply-avatar');
        if (replyAvatar) {
            replyAvatar.style.backgroundColor = 'var(--theme-blue)';
            replyAvatar.textContent = 'Y';
        }

        conversationPlaceholder.style.display = 'none';
        conversationWrapper.style.display = 'flex';
        conversationContent.style.display = 'flex'; // Ensure flex layout
        conversationContent.classList.remove('hidden');

        // Scroll to bottom
        conversationView.scrollTop = conversationView.scrollHeight;
        feather.replace();
    }

    function showListLoading() {
        inboxList.innerHTML = `< div class="outlook-loader" > <div class="spinner-circle"></div></div > `;
    }

    function toggleReplyBox(show) {
        if (show) {
            replyBox.style.display = 'flex';
            // Simple animation if gsap not present or just show
            replyBox.style.opacity = 0;
            setTimeout(() => replyBox.style.opacity = 1, 50);
            if (document.getElementById('reply-editor')) document.getElementById('reply-editor').focus();
        } else {
            replyBox.style.display = 'none';
            if (document.getElementById('reply-editor')) document.getElementById('reply-editor').value = '';
        }
    }

    // --- Action Logic ---
    async function loadMail(folder, page = 1) {
        showListLoading();
        try {
            const data = await api.getMail(folder, page);
            renderMailList(data);
            renderPagination(data);
        } catch (e) {
            inboxList.innerHTML = `< div style = "padding: 16px; color: red;" > Error: ${e.message}</div > `;
        }
    }

    async function loadConversationData(convId, msgId) {
        activeConversationId = convId;
        activeMessageId = msgId;

        // showLoading(false, true);
        toggleReplyBox(false);

        try {
            const data = await api.getConversation(convId);
            renderConversation(data);

            // Mark Read UI
            const item = document.querySelector(`.mail-item[data-id="${msgId}"]`);
            if (item && item.classList.contains('unread')) {
                item.classList.remove('unread');
                item.querySelector('.item-subject').style.fontWeight = 'normal';
                api.markRead(msgId);
            }
        } catch (e) {
            console.error('Error loading conversation:', e);
            conversationView.innerHTML = `<div style="padding:20px; text-align:center; color:#666;">Failed to load conversation.</div>`;
        }
    }

    // --- Event Listeners ---
    if (sidebar) {
        sidebar.addEventListener('click', e => {
            const row = e.target.closest('.folder-row');
            if (row) {
                e.preventDefault();
                document.querySelectorAll('.folder-row').forEach(r => r.classList.remove('active'));
                row.classList.add('active');

                // Reset detail view - show placeholder
                conversationWrapper.style.display = 'none';
                conversationPlaceholder.style.display = 'flex';

                const folder = row.dataset.folder;
                if (folder && folder !== currentFolder) {
                    currentFolder = folder;
                    currentPage = 1;
                    loadMail(currentFolder);
                }
            }
        });
    }

    inboxList.addEventListener('click', e => {
        const item = e.target.closest('.mail-item');
        if (item) {
            document.querySelectorAll('.mail-item').forEach(i => i.classList.remove('selected'));
            item.classList.add('selected');
            loadConversationData(item.dataset.conversationId, item.dataset.id);
        }
    });

    paginationContainer.addEventListener('click', e => {
        e.preventDefault();
        const link = e.target.closest('.page-link');
        if (link && !link.parentElement.classList.contains('disabled')) {
            const page = parseInt(link.dataset.page);
            if (!isNaN(page)) {
                currentPage = page;
                loadMail(currentFolder, currentPage);
            }
        }
    });

    if (refreshBtn) {
        refreshBtn.addEventListener('click', () => {
            loadMail(currentFolder, currentPage);
        });
    }

    if (replyBtn) replyBtn.addEventListener('click', () => toggleReplyBox(true));
    if (cancelReplyBtn) cancelReplyBtn.addEventListener('click', () => toggleReplyBox(false));

    if (sendReplyBtn) {
        sendReplyBtn.addEventListener('click', async () => {
            const body = document.getElementById('reply-editor').value;
            if (!body.trim() || !activeConversationId) return;
            const originalText = sendReplyBtn.innerHTML;
            sendReplyBtn.innerHTML = `Sending...`;
            sendReplyBtn.disabled = true;
            try {
                // api.sendReply implementation ...
                const result = await api.sendReply(activeConversationId, body);
                if (result.success) {
                    toggleReplyBox(false);
                    setTimeout(() => loadConversationData(activeConversationId, activeMessageId), 500);
                }
            } catch (error) {
                alert(error.message);
            } finally {
                sendReplyBtn.innerHTML = originalText;
                sendReplyBtn.disabled = false;
            }
        });
    }

    // Compose Modal Logic
    function openCompose() {
        if (composeModal) {
            composeModal.classList.add('active');
            composeModal.style.opacity = '1';
            composeModal.style.display = 'flex';
        }
    }
    function closeCompose() {
        if (composeModal) {
            composeModal.classList.remove('active');
            composeModal.style.display = 'none';
        }
    }

    if (composeBtnMain) composeBtnMain.addEventListener('click', openCompose);
    if (closeComposeBtn) closeComposeBtn.addEventListener('click', closeCompose);
    if (discardComposeBtn) discardComposeBtn.addEventListener('click', closeCompose);

    if (sendComposeBtn) {
        sendComposeBtn.addEventListener('click', async () => {
            const to = composeToInput.value;
            const sub = composeSubjectInput.value;
            const body = composeBodyInput.value;
            if (!to || !sub) return alert('To and Subject required');

            sendComposeBtn.textContent = 'Sending...';
            try {
                await api.sendMail(to, sub, body, false);
                closeCompose();
                alert('Sent!');
                if (currentFolder === 'sent') loadMail('sent', 1);
            } catch (e) {
                alert(e.message);
            } finally {
                sendComposeBtn.textContent = 'Send';
            }
        });
    }

    if (deleteBtn) {
        deleteBtn.addEventListener('click', async () => {
            if (!activeMessageId) return;
            if (!confirm('Are you sure you want to delete this message?')) return;
            try {
                await api.moveMessage(activeMessageId, 'trash');
                // Remove from list
                const item = document.querySelector(`.mail - item[data - id="${activeMessageId}"]`);
                if (item) item.remove();
                conversationWrapper.style.display = 'none';
                conversationPlaceholder.style.display = 'flex';
            } catch (e) {
                alert("Failed to delete: " + e.message);
            }
        });
    }

    // Initial Load
    loadMail('inbox');
});