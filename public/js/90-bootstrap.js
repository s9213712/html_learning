function bindAuthTabEvents() {
  const tabLogin = $("tab-login");
  const tabRegister = $("tab-register");
  if (tabLogin && tabLogin.dataset.authTabBound !== "1") {
    tabLogin.addEventListener("click", () => showTab("login"));
    tabLogin.dataset.authTabBound = "1";
  }
  if (tabRegister && tabRegister.dataset.authTabBound !== "1") {
    tabRegister.addEventListener("click", () => showTab("register"));
    tabRegister.dataset.authTabBound = "1";
  }
}

function bindAuthSubmitEvents() {
  const liBtn = $("li-btn");
  const regBtn = $("reg-btn");
  if (liBtn && liBtn.dataset.authSubmitBound !== "1") {
    liBtn.addEventListener("click", doLogin);
    liBtn.dataset.authSubmitBound = "1";
  }
  if (regBtn && regBtn.dataset.authSubmitBound !== "1") {
    regBtn.addEventListener("click", doRegister);
    regBtn.dataset.authSubmitBound = "1";
  }
}

function bindUiEvents() {
  if (typeof decorateSidebarMenu === "function") decorateSidebarMenu();
  if (typeof bindProfileFriendsControls === "function") bindProfileFriendsControls();
  if (typeof relocateSystemAdminSections === "function") relocateSystemAdminSections();
  const tabModuleChat = $("tab-module-chat");
  const tabModuleProfile = $("tab-module-profile");
  const tabModuleAnnouncements = $("tab-module-announcements");
  const tabModuleCommunity = $("tab-module-community");
  const tabModuleDrive = $("tab-module-drive");
  const tabModuleAlbums = $("tab-module-albums");
  const tabModuleVideos = $("tab-module-videos");
  const tabModuleGames = $("tab-module-games");
  const tabModuleExperiments = $("tab-module-experiments");
  const tabModuleJobs = $("tab-module-jobs");
  const tabModuleShares = $("tab-module-shares");
  const tabModuleComfyui = $("tab-module-comfyui");
  const tabModuleAiAgent = $("tab-module-ai-agent");
  const tabModuleEconomy = $("tab-module-economy");
  const tabModuleTrading = $("tab-module-trading");
  const tabModuleAccounts = $("tab-module-accounts");
  const tabModuleSystem = $("tab-module-system");
  const tabModuleServer = $("tab-module-server");
  const tabModuleAppeals = $("tab-module-appeals");
  const tabSystemHealth = $("tab-system-health");
  const tabSystemFeatures = $("tab-system-features");
  const tabSystemAppearance = $("tab-system-appearance");
  const tabSystemCore = $("tab-system-core");
  const tabSystemCapacity = $("tab-system-capacity");
  const tabSystemEnv = $("tab-system-env");
  const tabSystemLaunchCheck = $("tab-system-launch-check");
  const tabSystemBugReports = $("tab-system-bug-reports");
  const tabServerOverview = $("tab-server-overview");
  const tabServerServerMode = $("tab-server-server-mode");
  const tabServerAudit = $("tab-server-audit");
  const tabServerIntegrity = $("tab-server-integrity");
  const envRefresh = $("env-refresh-btn");
  const tabSettingsSecurity = $("tab-settings-security");
  const tabSettingsFeatures = $("tab-settings-features");
  const tabSettingsAppearance = $("tab-settings-appearance");
  const tabSettingsSystem = $("tab-settings-system");
  const tabUsers    = $("tab-users");
  const tabPasswordResets = $("tab-password-resets");
  const tabViol     = $("tab-violations");
  const tabGovernance = $("tab-governance");
  const tabMemberSettings = $("tab-member-settings");
  const tabNotices = $("tab-notices");
  const tabAppeals  = $("tab-appeals");
  const tabReports  = $("tab-reports");
  const recoveryToggle = $("recovery-toggle");
  const resetRequestBtn = $("reset-request-btn");
  const resetConfirmBtn = $("reset-confirm-btn");
  const verifyRequestBtn = $("verify-request-btn");
  const verifyConfirmBtn = $("verify-confirm-btn");
  const captchaRefresh = $("captcha-refresh");
  const logoutBtn   = $("logout-btn");
  const bugReportOpenBtn = $("bug-report-open-btn");
  const bugReportSubmitBtn = $("bug-report-submit-btn");
  const bugReportCancelBtn = $("bug-report-cancel-btn");
  const notificationToggle = $("notification-toggle");
  const notificationReadAll = $("notification-read-all");
  const selfEditBtn = $("self-edit-btn");
  const adminRefresh = $("admin-refresh");
  const adminBulkApproveBtn = $("admin-bulk-approve");
  const adminBulkRejectBtn = $("admin-bulk-reject");
  const adminUsersSearch = $("admin-user-search");
  const adminUsersPageSize = $("admin-users-page-size");
  const adminOpenAddBtn  = $("admin-open-add-user");
  const adminAddBtn  = $("admin-add-btn");
  const adminAddCancelBtn = $("admin-add-cancel");
  const auditRefresh = $("audit-refresh");
  const violRefresh  = $("violations-refresh");
  const governanceRefresh = $("governance-refresh");
  const governanceCreate = $("governance-create-proposal");
  const adminNoticeTemplate = $("admin-notice-template");
  const adminNoticeSendBtn = $("admin-notice-send-btn");
  const passwordResetReviewRefresh = $("password-reset-review-refresh");
  const passwordResetReviewStatus = $("password-reset-review-status");
  const appealSubmit = $("appeal-submit-btn");
  const appealRefresh = $("appeal-refresh-btn");
  const reportRefresh = $("admin-reports-refresh");
  const adminAppealsBulkApproveBtn = $("admin-appeals-bulk-approve");
  const adminAppealsBulkRejectBtn = $("admin-appeals-bulk-reject");
  const adminReportsBulkApproveBtn = $("admin-reports-bulk-approve");
  const adminReportsBulkRejectBtn = $("admin-reports-bulk-reject");
  const settingsSave = $("settings-save-btn");
  const driveRootStorageSettingsSave = $("drive-root-storage-settings-save-btn");
  const transmissionRpcTestBtn = $("transmission-rpc-test-btn");
  const settingsPanel = $("sec-server-settings");
  const serverTimeCheckBtn = $("server-time-check-btn");
  const comfyuiTestConnectionBtn = $("comfyui-test-connection-btn");
  const cloudDrivePolicySave = $("cloud-drive-policy-save-btn");
  const rootCatalogNew = $("root-catalog-new-btn");
  const rootCatalogRefresh = $("root-catalog-refresh-btn");
  const rootCatalogSave = $("root-catalog-save-btn");
  const rootTradingSettingsRefresh = $("root-trading-settings-refresh-btn");
  const rootTradingSettingsSave = $("root-trading-settings-save-btn");
  const rootTradingBtcTradeCheck = $("root-trading-btc-trade-check-btn");
  const rootTradingBtcTradeSetup = $("root-trading-btc-trade-setup-btn");
  const rootStorageRefresh = $("root-storage-refresh-btn");
  const rootStorageSave = $("root-storage-save-btn");
  const rootStorageClear = $("root-storage-clear-btn");
  const rootStorageUserSelect = $("root-storage-user-select");
  const rootStorageUsers = $("root-storage-users");
  const serverModeApply = $("server-mode-apply-btn");
  const serverUpdateRefresh = $("server-update-refresh-btn");
  const serverUpdatePreview = $("server-update-preview-btn");
  const internalTestTokenRefresh = $("internal-test-token-refresh-btn");
  const internalTestTokenRotate = $("internal-test-token-rotate-btn");
  const testerTokenCreate = $("tester-token-create-btn");
  const testerTokenList = $("tester-token-list-btn");
  const healthRefresh = $("health-refresh-btn");
  const pointsFinalitySweep = $("points-finality-sweep-btn");
  const launchCheckRefresh = $("launch-check-refresh-btn");
  const launchCheckBundle = $("launch-check-bundle-btn");
  const launchCheckArtifacts = $("launch-check-artifacts-btn");
  const launchCheckUploadFile = $("launch-check-upload-file");
  const launchCheckUploadSubmit = $("launch-check-upload-submit-btn");
  const launchCheckUploadClear = $("launch-check-upload-clear-btn");
  const integrityRefresh = $("integrity-refresh-btn");
  const integrityRescan = $("integrity-rescan-btn");
  const integrityExport = $("integrity-export-btn");
  const integrityBulkApprove = $("integrity-bulk-approve-btn");
  const integrityBulkReject = $("integrity-bulk-reject-btn");
  const integrityBulkIgnore = $("integrity-bulk-ignore-btn");
  const integrityRepair = $("integrity-repair-btn");
  const auditChainRepair = $("audit-chain-repair-btn");
  const restartBtn   = $("restart-server-btn");
  const securityCenterRefresh = $("security-center-refresh-btn");
  const securityControlsSave = $("security-controls-save-btn");
  const securityThresholdsSave = $("security-thresholds-save-btn");
  const securityModeApply = $("security-mode-apply-btn");
  const securityProfileSave = $("security-profile-save-btn");
  const securityProfileLoadCurrent = $("security-profile-load-current-btn");
  const securityModeSelect = $("security-mode-select");
  const securityTestRefresh = $("security-test-refresh-btn");
  const securityPentestStart = $("security-pentest-start-btn");
  const securityPrivilegeStart = $("security-privilege-start-btn");
  const securityFunctionalStart = $("security-functional-start-btn");
  const securityStressStart = $("security-stress-start-btn");
  const serverModeSelect = $("server-mode-select");
  const rootBugReportRefresh = $("root-bug-report-refresh-btn");
  const rootBugReportStatus = $("root-bug-report-status");
  const editSaveBtn = $("user-edit-save");
  const editCancelBtn = $("user-edit-cancel");
  const avatarUploadBtn = $("edit-user-avatar-upload");
  const chatCreateToggleBtn = $("chat-create-room-toggle-btn");
  const chatCreateCancelBtn = $("chat-create-room-cancel-btn");
  const chatCreateCloseBtn = $("chat-create-room-close-btn");
  const chatJoinOpenBtn = $("chat-join-room-open-btn");
  const chatJoinCancelBtn = $("chat-join-room-cancel-btn");
  const chatJoinCloseBtn = $("chat-join-room-close-btn");
  const chatCreateBtn = $("chat-create-room-btn");
  const chatJoinBtn = $("chat-join-room-btn");
  const chatRefreshRoomBtn = $("chat-room-refresh-btn");
  const chatRefreshMsgBtn = $("chat-refresh-msg-btn");
  const chatSendBtn = $("chat-send-btn");
  const chatFriendAddBtn = $("chat-friend-add-btn");
  const chatRoomInviteBtn = $("chat-room-invite-btn");
  const chatRoomExportBtn = $("chat-room-export-btn");
  const chatAttachmentPickBtn = $("chat-attachment-pick-btn");
  const chatAttachmentFile = $("chat-attachment-file");
  const chatAttachmentExistingSelect = $("chat-attachment-existing-file-id");
  const chatInput = $("chat-message-input");
  const communityAnnouncementBtn = $("community-announcement-submit");
  const communityAnnouncementOpenBtn = $("community-announcement-open-btn");
  const communityAnnouncementCancelBtn = $("community-announcement-cancel-btn");
  const announcementAttachmentUploadBtn = $("announcement-attachment-upload-btn");
  const announcementAttachmentExistingBtn = $("announcement-attachment-existing-btn");
  const communityCategoryCreateBtn = $("community-category-create-btn");
  const communityCategoryManagerOpenBtn = $("community-category-manager-open-btn");
  const communityCategoryManagerCloseBtn = $("community-category-manager-close-btn");
  const communityBoardRequestOpenBtn = $("community-board-request-open-btn");
  const communityBoardRequestCancelBtn = $("community-board-request-cancel-btn");
  const communityBoardRequestBtn = $("community-board-request-btn");
  const communityThreadCreateOpenBtn = $("community-thread-create-open-btn");
  const communityThreadCreateCancelBtn = $("community-thread-create-cancel-btn");
  const communityThreadSubmitBtn = $("community-thread-submit");
  const communityReplySubmitBtn = $("community-reply-submit");
  const communityThreadPrevBtn = $("community-thread-prev");
  const communityThreadNextBtn = $("community-thread-next");
  const communityThreadStickyToggle = $("community-thread-sticky-toggle");
  const communityThreadLockToggle = $("community-thread-lock-toggle");
  const communityThreadDeleteBtn = $("community-thread-delete-btn");
  const communityToolsToggleBtn = $("community-tools-toggle-btn");
  const communityReviewTabBtn = $("community-review-tab-btn");
  const communityModeratorOpenBtn = $("community-moderator-open-btn");
  const communityModeratorRefreshBtn = $("community-moderator-refresh-btn");
  const communityModeratorSaveBtn = $("community-moderator-save-btn");
  const communityModeratorPreset = $("community-moderator-preset");
  const communityBackBoardsBtn = $("community-back-boards-btn");
  const communityBackThreadsBtn = $("community-back-threads-btn");
  const communityBoardSearch = $("community-board-search");
  const communityThreadSearch = $("community-thread-search");
  const driveRefreshBtn = $("drive-refresh-btn");
  const driveUploadBtn = $("drive-upload-btn");
  const driveRemoteDownloadBtn = $("drive-remote-download-btn");
  const driveRemoteTorrentBtn = $("drive-remote-torrent-btn");
  const driveRemoteTorrentInlineBtn = $("drive-remote-torrent-inline-btn");
  const storageUploadBtn = $("storage-upload-btn");
  const storageFolderUploadBtn = $("storage-folder-upload-btn");
  const storageRefreshBtn = $("storage-refresh-btn");
  const storageUploadFile = $("storage-upload-file");
  const storageUploadFolder = $("storage-upload-folder");
  const driveRemoteTorrentFile = $("drive-remote-torrent-file");
  const storageFolderCreateBtn = $("storage-folder-create-btn");
  const storageOrganizeBtn = $("storage-organize-btn");
  const storageFolderMoveBtn = $("storage-folder-move-btn");
  const albumCreateBtn = $("album-create-btn");
  const albumThumbSize = $("album-thumb-size");
  const gameRefreshBtn = $("game-refresh-btn");
  const gameInviteBtn = $("game-invite-btn");
  const gamePracticeBtn = $("game-practice-btn");
  const gameResignBtn = $("game-resign-btn");
  const gameAwardBtn = $("game-award-btn");
  const jobCenterRefreshBtn = $("job-center-refresh-btn");
  const shareCenterRefreshBtn = $("share-center-refresh-btn");
  const comfyuiRefreshBtn = $("comfyui-refresh-btn");
  const comfyuiLoadDraftBtn = $("comfyui-load-draft-btn");
  const comfyuiStartBtn = $("comfyui-start-btn");
  const comfyuiStopBtn = $("comfyui-stop-btn");
  const comfyuiGenerateBtn = $("comfyui-generate-btn");
  const comfyuiInterruptBtn = $("comfyui-interrupt-btn");
  const comfyuiSaveBtn = $("comfyui-save-btn");
  const comfyuiShareBtn = $("comfyui-share-btn");
  const comfyuiDiscardBtn = $("comfyui-discard-btn");
  const comfyuiLoraAddBtn = $("comfyui-lora-add-btn");
  const comfyuiCivitaiInspectBtn = $("comfyui-civitai-inspect-btn");
  const comfyuiCivitaiVersion = $("comfyui-civitai-version");
  const comfyuiModelDownloadBtn = $("comfyui-model-download-btn");
  const aiAgentRefreshBtn = $("ai-agent-refresh-btn");
  const aiAgentSendBtn = $("ai-agent-send-btn");
  const aiAgentClearBtn = $("ai-agent-clear-btn");
  const aiAgentImageFile = $("ai-agent-image-file");
  const aiAgentAuditRefreshBtn = $("ai-agent-audit-refresh-btn");
  const aiAgentAuditScanBtn = $("ai-agent-audit-scan-btn");
  const economyRefreshBtn = $("economy-refresh-btn");
  const economyAdminRefreshBtn = $("economy-admin-refresh-btn");
  const economyAdjustBtn = $("economy-adjust-btn");
  const economyRootReportBtn = $("economy-root-report-btn");
  const economyRollbackBtn = $("economy-rollback-btn");
  const economySealBtn = $("economy-seal-btn");
  const economyVerifyBtn = $("economy-verify-btn");
  const sidebarToggle = $("sidebar-toggle");
  const userEditOverlay = $("user-edit-overlay");
  const adminAddOverlay = $("admin-add-overlay");
  const bugReportOverlay = $("bug-report-overlay");

  bindAuthTabEvents();
  if (tabModuleChat) tabModuleChat.addEventListener("click", () => switchModuleTab("chat"));
  if (tabModuleProfile) tabModuleProfile.addEventListener("click", () => switchModuleTab("profile"));
  if (tabModuleAnnouncements) tabModuleAnnouncements.addEventListener("click", () => switchModuleTab("announcements"));
  if (tabModuleCommunity) tabModuleCommunity.addEventListener("click", () => switchModuleTab("community"));
  if (tabModuleDrive) tabModuleDrive.addEventListener("click", () => switchModuleTab("drive"));
  if (tabModuleAlbums) tabModuleAlbums.addEventListener("click", () => switchModuleTab("albums"));
  if (tabModuleVideos) tabModuleVideos.addEventListener("click", () => {
    if (typeof openVideoOverview === "function") openVideoOverview();
    else switchModuleTab("videos");
  });
  if (tabModuleGames) tabModuleGames.addEventListener("click", () => switchModuleTab("games"));
  if (tabModuleExperiments) tabModuleExperiments.addEventListener("click", () => switchModuleTab("experiments"));
  if (tabModuleJobs) tabModuleJobs.addEventListener("click", () => switchModuleTab("jobs"));
  if (tabModuleShares) tabModuleShares.addEventListener("click", () => switchModuleTab("shares"));
  if (tabModuleComfyui) tabModuleComfyui.addEventListener("click", () => switchModuleTab("comfyui"));
  if (tabModuleAiAgent) tabModuleAiAgent.addEventListener("click", () => switchModuleTab("ai-agent"));
  if (tabModuleEconomy) tabModuleEconomy.addEventListener("click", () => switchModuleTab("economy"));
  if (tabModuleTrading) tabModuleTrading.addEventListener("click", () => {
    switchModuleTab("trading");
    if (typeof openTradingExchangePage === "function") openTradingExchangePage();
  });
  if (tabModuleAppeals) tabModuleAppeals.addEventListener("click", () => switchModuleTab("appeals"));
  if (tabModuleAccounts) tabModuleAccounts.addEventListener("click", () => switchModuleTab("accounts"));
  if (tabModuleSystem) tabModuleSystem.addEventListener("click", () => switchModuleTab("system"));
  if (tabModuleServer) tabModuleServer.addEventListener("click", () => switchModuleTab("server"));
  if (tabSystemHealth) tabSystemHealth.addEventListener("click", () => switchSystemTab("health"));
  if (tabSystemFeatures) tabSystemFeatures.addEventListener("click", () => switchSystemTab("features"));
  if (tabSystemAppearance) tabSystemAppearance.addEventListener("click", () => switchSystemTab("appearance"));
  if (tabSystemCore) tabSystemCore.addEventListener("click", () => switchSystemTab("core"));
  if (tabSystemCapacity) tabSystemCapacity.addEventListener("click", () => switchSystemTab("capacity"));
  if (tabSystemEnv) tabSystemEnv.addEventListener("click", () => switchSystemTab("env"));
  if (tabSystemLaunchCheck) tabSystemLaunchCheck.addEventListener("click", () => switchSystemTab("launch-check"));
  if (tabSystemBugReports) tabSystemBugReports.addEventListener("click", () => switchSystemTab("bug-reports"));
  if (tabServerOverview) tabServerOverview.addEventListener("click", () => switchServerTab("overview"));
  if (tabServerServerMode) tabServerServerMode.addEventListener("click", () => switchServerTab("server-mode"));
  if (tabServerAudit) tabServerAudit.addEventListener("click", () => switchServerTab("audit"));
  if (tabServerIntegrity) tabServerIntegrity.addEventListener("click", () => switchServerTab("integrity"));
  if (envRefresh) envRefresh.addEventListener("click", loadServerEnv);
  if (tabSettingsSecurity) tabSettingsSecurity.addEventListener("click", () => switchSettingsSection("security"));
  if (tabSettingsFeatures) tabSettingsFeatures.addEventListener("click", () => switchSettingsSection("features"));
  if (tabSettingsAppearance) tabSettingsAppearance.addEventListener("click", () => switchSettingsSection("appearance"));
  if (tabSettingsSystem) tabSettingsSystem.addEventListener("click", () => switchSettingsSection("system"));
  if (tabUsers)    tabUsers.addEventListener("click",    () => switchAdminTab("users"));
  if (tabPasswordResets) tabPasswordResets.addEventListener("click", () => switchAdminTab("password-resets"));
  if (tabViol)     tabViol.addEventListener("click",     () => switchAdminTab("violations"));
  if (tabGovernance) tabGovernance.addEventListener("click", () => switchAdminTab("governance"));
  if (tabMemberSettings) tabMemberSettings.addEventListener("click", () => switchAdminTab("member-settings"));
  if (tabNotices) tabNotices.addEventListener("click", () => switchAdminTab("notices"));
  if (tabAppeals)  tabAppeals.addEventListener("click",   () => switchAdminTab("appeals"));
  if (tabReports)  tabReports.addEventListener("click",   () => switchAdminTab("reports"));
  if (jobCenterRefreshBtn) jobCenterRefreshBtn.addEventListener("click", () => {
    if (typeof startJobCenterPolling === "function") startJobCenterPolling({ immediate: true, force: true });
    else if (typeof loadJobCenter === "function") loadJobCenter();
  });
  if (shareCenterRefreshBtn) shareCenterRefreshBtn.addEventListener("click", () => loadShareCenter());
  if (aiAgentRefreshBtn && typeof loadAiAgentStatus === "function") aiAgentRefreshBtn.addEventListener("click", () => loadAiAgentStatus({ force: true }));
  if (aiAgentSendBtn && typeof sendAiAgentMessage === "function") aiAgentSendBtn.addEventListener("click", () => sendAiAgentMessage());
  if (aiAgentClearBtn && typeof clearAiAgentConversation === "function") aiAgentClearBtn.addEventListener("click", () => clearAiAgentConversation());
  if (aiAgentAuditRefreshBtn && typeof loadAiAgentAuditStatus === "function") aiAgentAuditRefreshBtn.addEventListener("click", () => loadAiAgentAuditStatus());
  if (aiAgentAuditScanBtn && typeof runAiAgentAuditScan === "function") aiAgentAuditScanBtn.addEventListener("click", () => runAiAgentAuditScan());
  if (aiAgentImageFile && typeof handleAiAgentImagePick === "function") aiAgentImageFile.addEventListener("change", handleAiAgentImagePick);
  bindAuthSubmitEvents();
  if (typeof bindRegisterFieldHelpers === "function") bindRegisterFieldHelpers();
  if (typeof bindAuthRecoveryControls === "function") bindAuthRecoveryControls();
  else {
    if (recoveryToggle) recoveryToggle.addEventListener("click", toggleRecoveryPanel);
    if (resetRequestBtn) resetRequestBtn.addEventListener("click", requestPasswordReset);
    if (resetConfirmBtn) resetConfirmBtn.addEventListener("click", confirmPasswordReset);
    if (verifyRequestBtn) verifyRequestBtn.addEventListener("click", requestEmailVerification);
    if (verifyConfirmBtn) verifyConfirmBtn.addEventListener("click", confirmEmailVerification);
  }
  if (captchaRefresh) captchaRefresh.addEventListener("click", loadCaptchaChallenge);
  if (logoutBtn)  logoutBtn.addEventListener("click",    doLogout);
  if (bugReportOpenBtn) bugReportOpenBtn.addEventListener("click", showBugReportDialog);
  if (bugReportSubmitBtn) bugReportSubmitBtn.addEventListener("click", submitBugReport);
  if (bugReportCancelBtn) bugReportCancelBtn.addEventListener("click", hideBugReportDialog);
  if (notificationToggle) notificationToggle.addEventListener("click", toggleNotificationPanel);
  if (notificationReadAll) notificationReadAll.addEventListener("click", markAllNotificationsRead);
  if (selfEditBtn) selfEditBtn.addEventListener("click", () => {
    if (typeof openMyProfilePanel === "function") openMyProfilePanel("edit");
    else if (currentUserId) editUser(currentUserId);
  });
  if (adminRefresh) adminRefresh.addEventListener("click", () => loadUsers(adminUsersPage));
  if ($("admin-users-prev")) $("admin-users-prev").addEventListener("click", () => loadUsers(Math.max(1, adminUsersPage - 1)));
  if ($("admin-users-next")) $("admin-users-next").addEventListener("click", () => loadUsers(adminUsersPage + 1));
  if (adminUsersPageSize) adminUsersPageSize.addEventListener("change", () => loadUsers(1));
  if (adminUsersSearch) {
    let adminUsersSearchTimer = null;
    adminUsersSearch.addEventListener("input", () => {
      if (adminUsersSearchTimer) clearTimeout(adminUsersSearchTimer);
      adminUsersSearchTimer = setTimeout(() => loadUsers(1), 250);
    });
  }
  if (adminBulkApproveBtn) adminBulkApproveBtn.addEventListener("click", () => bulkReviewRegistrations("approve"));
  if (adminBulkRejectBtn) adminBulkRejectBtn.addEventListener("click", () => bulkReviewRegistrations("reject"));
  if (passwordResetReviewRefresh) passwordResetReviewRefresh.addEventListener("click", loadPasswordResetReviews);
  if (passwordResetReviewStatus) passwordResetReviewStatus.addEventListener("change", loadPasswordResetReviews);
  if (adminOpenAddBtn) adminOpenAddBtn.addEventListener("click", showAdminAddDialog);
  if (adminAddBtn)  adminAddBtn.addEventListener("click",  createUserByAdmin);
  if (adminAddCancelBtn) adminAddCancelBtn.addEventListener("click", hideAdminAddDialog);
  if (chatCreateToggleBtn) chatCreateToggleBtn.addEventListener("click", toggleChatCreatePanel);
  if (chatCreateCancelBtn) chatCreateCancelBtn.addEventListener("click", () => setChatCreatePanelVisible(false));
  if (chatCreateCloseBtn) chatCreateCloseBtn.addEventListener("click", () => setChatCreatePanelVisible(false));
  if (chatJoinOpenBtn) chatJoinOpenBtn.addEventListener("click", toggleChatJoinPanel);
  if (chatJoinCancelBtn) chatJoinCancelBtn.addEventListener("click", () => setChatJoinPanelVisible(false));
  if (chatJoinCloseBtn) chatJoinCloseBtn.addEventListener("click", () => setChatJoinPanelVisible(false));
  if (chatCreateBtn) chatCreateBtn.addEventListener("click", createChatRoom);
  if (chatJoinBtn) chatJoinBtn.addEventListener("click", joinChatRoom);
  if (chatRefreshRoomBtn) chatRefreshRoomBtn.addEventListener("click", loadChatRooms);
  if (chatRefreshMsgBtn) chatRefreshMsgBtn.addEventListener("click", () => {
    if (selectedChatRoomId) loadChatMessages(selectedChatRoomId, false);
  });
  if (chatSendBtn) chatSendBtn.addEventListener("click", sendChatMessage);
  if (chatFriendAddBtn) chatFriendAddBtn.addEventListener("click", addChatFriend);
  if (chatRoomInviteBtn) chatRoomInviteBtn.addEventListener("click", inviteChatRoomMembers);
  if (chatRoomExportBtn) chatRoomExportBtn.addEventListener("click", exportChatRoom);
  document.querySelectorAll("[data-chat-sticker]").forEach((btn) => {
    btn.addEventListener("click", () => insertChatSticker(btn.dataset.chatSticker || ""));
  });
  if (chatAttachmentPickBtn) chatAttachmentPickBtn.addEventListener("click", openChatAttachmentPicker);
  if (chatAttachmentFile) chatAttachmentFile.addEventListener("change", uploadChatAttachment);
  if (chatAttachmentExistingSelect) chatAttachmentExistingSelect.addEventListener("change", attachExistingChatFile);
  if (communityAnnouncementBtn) communityAnnouncementBtn.addEventListener("click", publishAnnouncement);
  if (communityAnnouncementOpenBtn) communityAnnouncementOpenBtn.addEventListener("click", () => toggleCommunityAnnouncementEditor(true));
  if (communityAnnouncementCancelBtn) communityAnnouncementCancelBtn.addEventListener("click", () => toggleCommunityAnnouncementEditor(false));
  if (announcementAttachmentUploadBtn) announcementAttachmentUploadBtn.addEventListener("click", uploadAnnouncementAttachmentRequest);
  if (announcementAttachmentExistingBtn) announcementAttachmentExistingBtn.addEventListener("click", attachExistingAnnouncementFile);
  if (communityCategoryCreateBtn) communityCategoryCreateBtn.addEventListener("click", createCommunityCategory);
  if (communityCategoryManagerOpenBtn) communityCategoryManagerOpenBtn.addEventListener("click", () => toggleCommunityCategoryManager(true));
  if (communityCategoryManagerCloseBtn) communityCategoryManagerCloseBtn.addEventListener("click", () => toggleCommunityCategoryManager(false));
  if (communityBoardRequestOpenBtn) communityBoardRequestOpenBtn.addEventListener("click", () => toggleCommunityBoardRequest(true));
  if (communityBoardRequestCancelBtn) communityBoardRequestCancelBtn.addEventListener("click", () => toggleCommunityBoardRequest(false));
  if (communityBoardRequestBtn) communityBoardRequestBtn.addEventListener("click", requestCommunityBoard);
  if (communityThreadCreateOpenBtn) communityThreadCreateOpenBtn.addEventListener("click", () => toggleCommunityThreadCreator(true));
  if (communityThreadCreateCancelBtn) communityThreadCreateCancelBtn.addEventListener("click", () => toggleCommunityThreadCreator(false));
  if (communityThreadSubmitBtn) communityThreadSubmitBtn.addEventListener("click", createCommunityThread);
  if (communityReplySubmitBtn) communityReplySubmitBtn.addEventListener("click", replyCommunityThread);
  document.querySelectorAll("[data-community-media-picker]").forEach((btn) => {
    btn.addEventListener("click", () => openCommunityInlineMediaPicker(btn));
  });
  document.querySelectorAll("input[data-community-media-input]").forEach((input) => {
    input.addEventListener("change", () => uploadCommunityInlineMedia(input));
  });
  if (communityThreadPrevBtn) communityThreadPrevBtn.addEventListener("click", () => {
    if (!selectedCommunityBoardId || communityThreadPage <= 0) return;
    communityThreadPage -= 1;
    openCommunityBoard(selectedCommunityBoardId);
  });
  if (communityThreadNextBtn) communityThreadNextBtn.addEventListener("click", () => {
    if (!selectedCommunityBoardId) return;
    communityThreadPage += 1;
    openCommunityBoard(selectedCommunityBoardId);
  });
  if (communityThreadStickyToggle) communityThreadStickyToggle.addEventListener("click", toggleCommunityThreadSticky);
  if (communityThreadLockToggle) communityThreadLockToggle.addEventListener("click", toggleCommunityThreadLock);
  if (communityThreadDeleteBtn) communityThreadDeleteBtn.addEventListener("click", deleteCommunityThread);
  if (communityModeratorOpenBtn) communityModeratorOpenBtn.addEventListener("click", () => toggleCommunityModeratorManager());
  if (communityModeratorRefreshBtn) communityModeratorRefreshBtn.addEventListener("click", () => loadCommunityModerators());
  if (communityModeratorSaveBtn) communityModeratorSaveBtn.addEventListener("click", saveCommunityModerator);
  if (communityModeratorPreset) communityModeratorPreset.addEventListener("change", () => applyModeratorPreset(communityModeratorPreset.value));
  if (communityToolsToggleBtn) communityToolsToggleBtn.addEventListener("click", () => toggleCommunityTools());
  if (communityReviewTabBtn) communityReviewTabBtn.addEventListener("click", () => switchCommunityMode(communityMode === "review" ? "boards" : "review"));
  if (communityBackBoardsBtn) communityBackBoardsBtn.addEventListener("click", showCommunityBoardStage);
  if (communityBackThreadsBtn) communityBackThreadsBtn.addEventListener("click", showCommunityThreadStage);
  if (communityBoardSearch) communityBoardSearch.addEventListener("input", (e) => {
    communityBoardQuery = e?.target?.value || "";
    renderCommunityBoards();
  });
  if (communityThreadSearch) communityThreadSearch.addEventListener("input", (e) => {
    communityThreadQuery = e?.target?.value || "";
    communityThreadPage = 0;
    if (selectedCommunityBoardId) openCommunityBoard(selectedCommunityBoardId);
  });
  if (driveRefreshBtn) driveRefreshBtn.addEventListener("click", loadDriveDashboard);
  if (driveUploadBtn) driveUploadBtn.addEventListener("click", uploadDriveFile);
  if (driveRemoteDownloadBtn) driveRemoteDownloadBtn.addEventListener("click", promptRemoteDriveDownloadUrl);
  if (driveRemoteTorrentBtn) driveRemoteTorrentBtn.addEventListener("click", openRemoteTorrentPicker);
  if (driveRemoteTorrentInlineBtn) driveRemoteTorrentInlineBtn.addEventListener("click", openRemoteTorrentPicker);
  if (storageUploadBtn) storageUploadBtn.addEventListener("click", openStorageUploadPicker);
  if (storageFolderUploadBtn) storageFolderUploadBtn.addEventListener("click", openStorageFolderUploadPicker);
  if (storageUploadFile) storageUploadFile.addEventListener("change", uploadStorageFile);
  if (storageUploadFolder) storageUploadFolder.addEventListener("change", uploadStorageFolder);
  if (driveRemoteTorrentFile) driveRemoteTorrentFile.addEventListener("change", () => startRemoteDriveDownload({ source: "torrent", triggerButton: driveRemoteTorrentInlineBtn || driveRemoteTorrentBtn }));
  if (storageRefreshBtn) storageRefreshBtn.addEventListener("click", loadDriveDashboard);
  if (storageFolderCreateBtn) storageFolderCreateBtn.addEventListener("click", createStorageFolder);
  if (storageOrganizeBtn) storageOrganizeBtn.addEventListener("click", organizeSelectedStorageFile);
  if (storageFolderMoveBtn) storageFolderMoveBtn.addEventListener("click", moveStorageFolder);
  if (albumCreateBtn) albumCreateBtn.addEventListener("click", createAlbum);
  if (albumThumbSize) {
    if (typeof getAlbumThumbSize === "function") albumThumbSize.value = getAlbumThumbSize();
    albumThumbSize.addEventListener("change", (event) => setAlbumThumbSize(event.target.value));
  }
  if (gameRefreshBtn) gameRefreshBtn.addEventListener("click", loadGameZone);
  if (gameInviteBtn) gameInviteBtn.addEventListener("click", createGameInvite);
  if (gamePracticeBtn) gamePracticeBtn.addEventListener("click", createPracticeGame);
  if (gameResignBtn) gameResignBtn.addEventListener("click", resignGame);
  if (gameAwardBtn) gameAwardBtn.addEventListener("click", awardGameRewards);
  if (comfyuiRefreshBtn) comfyuiRefreshBtn.addEventListener("click", () => loadComfyuiModels({ forceRefresh: true }));
  if (comfyuiLoadDraftBtn) comfyuiLoadDraftBtn.addEventListener("click", loadComfyuiLastSettings);
  if (comfyuiStartBtn) comfyuiStartBtn.addEventListener("click", startLocalComfyui);
  if (comfyuiStopBtn) comfyuiStopBtn.addEventListener("click", stopLocalComfyui);
  if (comfyuiGenerateBtn) comfyuiGenerateBtn.addEventListener("click", generateComfyuiImage);
  if (comfyuiInterruptBtn) comfyuiInterruptBtn.addEventListener("click", interruptComfyuiGeneration);
  if (comfyuiSaveBtn) comfyuiSaveBtn.addEventListener("click", saveComfyuiImageToDrive);
  if (comfyuiShareBtn) comfyuiShareBtn.addEventListener("click", shareComfyuiToCommunity);
  if (comfyuiDiscardBtn) comfyuiDiscardBtn.addEventListener("click", discardComfyuiImage);
  if (comfyuiLoraAddBtn) comfyuiLoraAddBtn.addEventListener("click", addSelectedComfyuiLora);
  if (comfyuiCivitaiInspectBtn) comfyuiCivitaiInspectBtn.addEventListener("click", inspectComfyuiCivitaiModel);
  if (comfyuiCivitaiVersion) comfyuiCivitaiVersion.addEventListener("change", onComfyuiCivitaiVersionChange);
  if (comfyuiModelDownloadBtn) comfyuiModelDownloadBtn.addEventListener("click", downloadComfyuiModelFromUrl);
  if (rootTradingSettingsRefresh) rootTradingSettingsRefresh.addEventListener("click", loadRootTradingSettings);
  if (rootTradingSettingsSave) rootTradingSettingsSave.addEventListener("click", saveRootTradingSettings);
  if (rootTradingBtcTradeCheck) rootTradingBtcTradeCheck.addEventListener("click", checkRootBtcTradeStatus);
  if (rootTradingBtcTradeSetup) rootTradingBtcTradeSetup.addEventListener("click", setupRootBtcTrade);
  if (typeof bindComfyuiDraftPersistence === "function") bindComfyuiDraftPersistence();
  if (typeof bindComfyuiAdvancedUi === "function") bindComfyuiAdvancedUi();
  if (typeof resetComfyuiIdleUi === "function") resetComfyuiIdleUi();
  if (typeof bindEconomyInlineEvents === "function") bindEconomyInlineEvents();
  if (sidebarToggle) sidebarToggle.addEventListener("click", () => {
    setSidebarCollapsed(!document.body.classList.contains("sidebar-collapsed"));
  });
  const sidebarNav = $("module-main-tabs");
  if (sidebarNav) {
    sidebarNav.addEventListener("click", (event) => {
      const sub = event.target?.closest?.("[data-sidebar-action]");
      if (!sub) return;
      event.preventDefault();
      event.stopPropagation();
      runSidebarAction(sub.dataset.sidebarAction || "");
    });
  }
  if (editSaveBtn)   editSaveBtn.addEventListener("click", submitEditUser);
  if (editCancelBtn) editCancelBtn.addEventListener("click", hideUserEditDialog);
  if (avatarUploadBtn) avatarUploadBtn.addEventListener("click", uploadUserAvatar);
  if (typeof bindAvatarCropperUi === "function") bindAvatarCropperUi();
  if ($("theme-quick-toggle")) $("theme-quick-toggle").addEventListener("click", toggleUserThemeModeQuickly);
  if ($("edit-user-theme-mode")) $("edit-user-theme-mode").addEventListener("change", applyUserThemeModeSelection);
  if ($("edit-user-appearance-preset")) $("edit-user-appearance-preset").addEventListener("change", applyUserAppearancePresetSelection);
  if ($("edit-user-appearance-reset")) $("edit-user-appearance-reset").addEventListener("click", resetUserAppearanceEditorToGlobal);
  Object.values(typeof USER_APPEARANCE_FIELD_MAP === "object" ? USER_APPEARANCE_FIELD_MAP : {}).forEach((id) => {
    const el = $(id);
    if (!el) return;
    const handler = () => markUserAppearanceEditorChanged();
    el.addEventListener("input", handler);
    el.addEventListener("change", handler);
  });
  if (userEditOverlay) userEditOverlay.addEventListener("click", (e) => {
    if (e.target === userEditOverlay) hideUserEditDialog();
  });
  if (adminAddOverlay) adminAddOverlay.addEventListener("click", (e) => {
    if (e.target === adminAddOverlay) hideAdminAddDialog();
  });
  if (bugReportOverlay) bugReportOverlay.addEventListener("click", (e) => {
    if (e.target === bugReportOverlay) hideBugReportDialog();
  });
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      hideUserEditDialog();
      hideAdminAddDialog();
      hideBugReportDialog();
      closeNotificationPanel();
    }
  });
  if (chatInput) chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      sendChatMessage();
    }
  });
  // Audit pagination
  if (auditRefresh) auditRefresh.addEventListener("click", () => loadAudit(auditPage));
  if ($("audit-prev")) $("audit-prev").addEventListener("click", () => loadAudit(Math.max(0, auditPage - 1)));
  if ($("audit-next")) $("audit-next").addEventListener("click", () => loadAudit(auditPage + 1));

  if (appealSubmit) appealSubmit.addEventListener("click", submitAppeal);
  if (appealRefresh) appealRefresh.addEventListener("click", loadUserAppeals);
  if ($("admin-appeal-status")) $("admin-appeal-status").addEventListener("change", (e) => {
    adminAppealStatus = e?.target?.value || "pending";
    loadAdminAppeals(1, adminAppealStatus);
  });
  if ($("admin-appeals-prev")) $("admin-appeals-prev").addEventListener("click", () => loadAdminAppeals(Math.max(1, adminAppealPage - 1), adminAppealStatus));
  if ($("admin-appeals-next")) $("admin-appeals-next").addEventListener("click", () => loadAdminAppeals(adminAppealPage + 1, adminAppealStatus));
  if ($("admin-appeals-refresh")) $("admin-appeals-refresh").addEventListener("click", () => loadAdminAppeals(adminAppealPage, adminAppealStatus));
  if (adminAppealsBulkApproveBtn) adminAppealsBulkApproveBtn.addEventListener("click", () => bulkReviewAppeals("approve"));
  if (adminAppealsBulkRejectBtn) adminAppealsBulkRejectBtn.addEventListener("click", () => bulkReviewAppeals("reject"));
  if ($("admin-report-status")) $("admin-report-status").addEventListener("change", (e) => {
    adminReportStatus = e?.target?.value || "pending";
    loadAdminReports(0, adminReportStatus);
  });
  if ($("admin-reports-prev")) $("admin-reports-prev").addEventListener("click", () => loadAdminReports(Math.max(0, adminReportPage - 1), adminReportStatus));
  if ($("admin-reports-next")) $("admin-reports-next").addEventListener("click", () => loadAdminReports(adminReportPage + 1, adminReportStatus));
  if (reportRefresh) reportRefresh.addEventListener("click", () => loadAdminReports(adminReportPage, adminReportStatus));
  if (adminReportsBulkApproveBtn) adminReportsBulkApproveBtn.addEventListener("click", () => bulkReviewMessageReports("approve"));
  if (adminReportsBulkRejectBtn) adminReportsBulkRejectBtn.addEventListener("click", () => bulkReviewMessageReports("reject"));

  // Violations
  if (violRefresh) violRefresh.addEventListener("click", () => loadViolations(violationsPage, violationTargetUser));
  if ($("violations-prev")) $("violations-prev").addEventListener("click", () => loadViolations(Math.max(0, violationsPage - 1), violationTargetUser));
  if ($("violations-next")) $("violations-next").addEventListener("click", () => loadViolations(violationsPage + 1, violationTargetUser));
  if ($("violation-user-select")) $("violation-user-select").addEventListener("change", (event) => loadViolations(0, event.target?.value || ""));
  if ($("violation-fine-status")) $("violation-fine-status").addEventListener("change", loadAdminViolationFines);
  if ($("violation-fines-refresh")) $("violation-fines-refresh").addEventListener("click", loadAdminViolationFines);
  if (governanceRefresh) governanceRefresh.addEventListener("click", loadGovernanceDashboard);
  if (governanceCreate) governanceCreate.addEventListener("click", createGovernanceProposal);
  if (adminNoticeTemplate) adminNoticeTemplate.addEventListener("change", applyAdminNoticeTemplate);
  if (adminNoticeSendBtn) adminNoticeSendBtn.addEventListener("click", sendAdminNotice);
  if ($("governance-action-type")) $("governance-action-type").addEventListener("change", updateGovernanceActionValueHelp);
  if ($("governance-target-user-id")) $("governance-target-user-id").addEventListener("change", updateGovernanceActionValueHelp);
  if ($("governance-emergency-execute")) $("governance-emergency-execute").addEventListener("change", updateGovernanceActionValueHelp);
  if ($("governance-proposal-status")) $("governance-proposal-status").addEventListener("change", loadGovernanceProposals);

  // Settings
  if (settingsSave) settingsSave.addEventListener("click", saveSettings);
  document.querySelectorAll("[data-settings-save-action]").forEach((btn) => {
    if (btn.dataset.settingsSaveBound === "1") return;
    btn.dataset.settingsSaveBound = "1";
    btn.addEventListener("click", saveSettings);
  });
  if (settingsPanel) {
    const clearHandler = () => {
      if (typeof clearSettingsStatus === "function") clearSettingsStatus();
    };
    settingsPanel.querySelectorAll("input, select, textarea").forEach((el) => {
      el.addEventListener("input", clearHandler);
      el.addEventListener("change", clearHandler);
    });
  }
  if (serverTimeCheckBtn) serverTimeCheckBtn.addEventListener("click", refreshServerTimeStatus);
  if (comfyuiTestConnectionBtn) comfyuiTestConnectionBtn.addEventListener("click", testComfyuiConnection);
  if ($("s-captcha-mode")) $("s-captcha-mode").addEventListener("change", updateCaptchaModeFields);
  if ($("s-comfyui-connection-mode")) $("s-comfyui-connection-mode").addEventListener("change", updateComfyuiConnectionModeFields);
  if ($("s-comfyui-comfyui-backend-mode")) $("s-comfyui-comfyui-backend-mode").addEventListener("change", (event) => setComfyuiBackendMode(event.target.value));
  document.querySelectorAll("[data-comfyui-settings-family]").forEach((button) => {
    button.addEventListener("click", () => setComfyuiSettingsFamily(button.dataset.comfyuiSettingsFamily || "comfyui"));
  });
  if ($("s-server-backpressure-mode")) $("s-server-backpressure-mode").addEventListener("change", updateBackpressureModeFields);
  if (typeof bindSettingsAssistants === "function") bindSettingsAssistants();
  if (cloudDrivePolicySave) cloudDrivePolicySave.addEventListener("click", saveCloudDriveAdminPolicy);
  if (driveRootStorageSettingsSave) driveRootStorageSettingsSave.addEventListener("click", saveDriveRootStorageSettings);
  if (transmissionRpcTestBtn) transmissionRpcTestBtn.addEventListener("click", testTransmissionRpcConnection);
  if (rootCatalogNew) rootCatalogNew.addEventListener("click", clearRootCatalogForm);
  if (rootCatalogRefresh) rootCatalogRefresh.addEventListener("click", loadRootEconomyCatalog);
  if (rootCatalogSave) rootCatalogSave.addEventListener("click", saveRootEconomyCatalogItem);
  if (rootTradingSettingsRefresh) rootTradingSettingsRefresh.addEventListener("click", loadRootTradingSettings);
  if (rootTradingSettingsSave) rootTradingSettingsSave.addEventListener("click", saveRootTradingSettings);
  if (rootStorageRefresh) rootStorageRefresh.addEventListener("click", loadRootStorageUsers);
  if (rootStorageSave) rootStorageSave.addEventListener("click", saveRootStorageOverride);
  if (rootStorageClear) rootStorageClear.addEventListener("click", clearRootStorageOverride);
  if (rootStorageUserSelect) rootStorageUserSelect.addEventListener("change", (event) => fillRootStorageOverrideForm(event.target.value));
  if (rootStorageUsers) rootStorageUsers.addEventListener("click", (event) => {
    const button = event.target?.closest?.("[data-root-storage-select]");
    if (!button) return;
    fillRootStorageOverrideForm(button.dataset.rootStorageSelect || "");
  });
  if (serverModeApply) serverModeApply.addEventListener("click", applyServerMode);
  if (serverUpdateRefresh) serverUpdateRefresh.addEventListener("click", () => loadServerUpdateStatus(true));
  if (serverUpdatePreview) serverUpdatePreview.addEventListener("click", previewServerUpdate);
  if (internalTestTokenRefresh) internalTestTokenRefresh.addEventListener("click", loadInternalTestTokenStatus);
  if (internalTestTokenRotate) internalTestTokenRotate.addEventListener("click", rotateInternalTestToken);
  if (testerTokenCreate) testerTokenCreate.addEventListener("click", createTesterToken);
  if (testerTokenList) testerTokenList.addEventListener("click", loadTesterTokens);
  if (healthRefresh) healthRefresh.addEventListener("click", loadServerHealth);
  if (pointsFinalitySweep) pointsFinalitySweep.addEventListener("click", startPointsFinalitySweep);
  if (integrityRefresh) integrityRefresh.addEventListener("click", loadIntegrityGuard);
  if (launchCheckRefresh) launchCheckRefresh.addEventListener("click", () => loadLaunchCheck());
  if (launchCheckBundle) launchCheckBundle.addEventListener("click", () => createLaunchCheckReleaseBundle());
  if (launchCheckArtifacts) launchCheckArtifacts.addEventListener("click", () => refreshLaunchCheckQaArtifacts());
  if (launchCheckUploadFile) launchCheckUploadFile.addEventListener("change", async () => {
    const file = launchCheckUploadFile.files && launchCheckUploadFile.files[0];
    if (!file) return;
    const textarea = $("launch-check-upload-json");
    const sub = $("launch-check-upload-sub");
    try {
      const text = await file.text();
      if (textarea) textarea.value = text;
      if (sub) sub.textContent = `已載入 ${file.name}，請確認 JSON 後再上傳。`;
      if (typeof launchCheckSetUploadStatus === "function") launchCheckSetUploadStatus("");
    } catch (err) {
      if (typeof launchCheckSetUploadStatus === "function") launchCheckSetUploadStatus(`讀取檔案失敗：${err && err.message ? err.message : "未知錯誤"}`, false);
    }
  });
  if (launchCheckUploadSubmit) launchCheckUploadSubmit.addEventListener("click", () => submitLaunchCheckReportUpload());
  if (launchCheckUploadClear) launchCheckUploadClear.addEventListener("click", () => {
    const textarea = $("launch-check-upload-json");
    const file = $("launch-check-upload-file");
    const sub = $("launch-check-upload-sub");
    if (textarea) textarea.value = "";
    if (file) file.value = "";
    if (sub) sub.textContent = "選擇 report 類型後，可貼上 JSON 或上傳 `.json` 檔。";
    if (typeof launchCheckSetUploadStatus === "function") launchCheckSetUploadStatus("");
  });
  if (integrityRescan) integrityRescan.addEventListener("click", rescanIntegrityGuard);
  if (integrityExport) integrityExport.addEventListener("click", exportIntegrityReport);
  if (integrityBulkApprove) integrityBulkApprove.addEventListener("click", () => reviewSelectedIntegrityFindings("approve"));
  if (integrityBulkReject) integrityBulkReject.addEventListener("click", () => reviewSelectedIntegrityFindings("reject"));
  if (integrityBulkIgnore) integrityBulkIgnore.addEventListener("click", () => reviewSelectedIntegrityFindings("ignore"));
  if (integrityRepair) integrityRepair.addEventListener("click", repairIntegrityChains);
  if (auditChainRepair) auditChainRepair.addEventListener("click", repairIntegrityChains);
  if (restartBtn)   restartBtn.addEventListener("click",   restartServer);
  if (securityCenterRefresh) securityCenterRefresh.addEventListener("click", loadSecurityCenter);
  if (securityControlsSave) securityControlsSave.addEventListener("click", saveSecurityCenterControls);
  if (securityThresholdsSave) securityThresholdsSave.addEventListener("click", saveSecurityThresholds);
  if (securityModeApply) securityModeApply.addEventListener("click", applySecurityMode);
  if (securityProfileSave) securityProfileSave.addEventListener("click", saveSecurityProfile);
  if (securityProfileLoadCurrent) securityProfileLoadCurrent.addEventListener("click", loadCurrentSecurityProfileDraft);
  if (securityModeSelect) securityModeSelect.addEventListener("change", () => previewSecurityProfileSelection("security-mode-select", "security-mode-profile-preview", "sc"));
  if (securityTestRefresh) securityTestRefresh.addEventListener("click", loadSecurityTestJobs);
  if (securityPentestStart) securityPentestStart.addEventListener("click", startSecurityPentest);
  if (securityPrivilegeStart) securityPrivilegeStart.addEventListener("click", startSecurityPrivilegeTest);
  if (securityFunctionalStart) securityFunctionalStart.addEventListener("click", startSecurityFunctionalSmoke);
  if (securityStressStart) securityStressStart.addEventListener("click", startSecurityStressTest);
  if (serverModeSelect) serverModeSelect.addEventListener("change", () => {
    previewSecurityProfileSelection("server-mode-select", "server-mode-profile-preview", "s");
    if (typeof updateServerModeTokenPanels === "function") updateServerModeTokenPanels(serverModeSelect.value);
    if (typeof updateServerModeLaunchCheckVisibility === "function") updateServerModeLaunchCheckVisibility();
  });
  if (rootBugReportRefresh) rootBugReportRefresh.addEventListener("click", loadRootBugReports);
  if (rootBugReportStatus) rootBugReportStatus.addEventListener("change", renderRootBugReportList);
}

$("li-pw").addEventListener("keydown", (e) => {
  if (e.key === "Enter") doLogin();
});
const loginTokenInput = $("li-internal-test-token");
if (loginTokenInput) loginTokenInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") doLogin();
});
$("reg-pw").addEventListener("keydown", (e) => {
  if (e.key === "Enter") doRegister();
});
if (typeof bindAuthRecoveryControls === "function") bindAuthRecoveryControls();

(async function init() {
  bindAuthTabEvents();
  bindAuthSubmitEvents();
  await loadSiteConfig();
  setupInactivityTracking();
  startServerConnectionMonitor();
  setCsrfToken(readCookie("csrf_token"));
  bindUiEvents();
  if (typeof loadCaptchaChallenge === "function") loadCaptchaChallenge();
  // 帶 timeout 的 fetch，避免 server 無回應時 UI 卡死
  async function safeFetch(url, opts = {}) {
    const ctrl = new AbortController();
    const id = setTimeout(() => ctrl.abort(), 5000);
    try {
      const res = await fetch(url, { ...opts, signal: ctrl.signal });
      clearTimeout(id);
      return res;
    } catch (e) {
      clearTimeout(id);
      throw e;
    }
  }
  try {
    if (typeof hasIdleTimeoutLogoutPending === "function" && hasIdleTimeoutLogoutPending()) {
      if (typeof forceIdleTimeoutLogout === "function") await forceIdleTimeoutLogout();
      return;
    }
    const res = await safeFetch(API + "/me?optional=1", { credentials: "same-origin" });
    const json = await res.json().catch(() => ({}));
    if (json.ok) {
      setAuthState(json);
    } else {
      try {
        localStorage.removeItem(AUTH_SESSION_HINT_STORAGE_KEY);
      } catch (_) {}
    }
  } catch (_) { /* 網路問題或 timeout，不影響操作 */ }
})();
