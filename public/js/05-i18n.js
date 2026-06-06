'use strict';

(function setupHackmeI18n() {
  const STORAGE_KEY = 'hackme_web.locale';
  const DEFAULT_LOCALE = 'zh-TW';
  const SUPPORTED_LOCALES = Object.freeze({
    'zh-TW': '繁體中文',
    en: 'English',
  });
  const ATTRIBUTE_NAMES = ['placeholder', 'title', 'aria-label', 'alt'];
  const SKIP_SELECTOR = [
    'script',
    'style',
    'template',
    'pre',
    'code',
    '[data-i18n-skip]',
    '[contenteditable="true"]',
    '.markdown-body',
    '.drive-preview-text',
    '.drive-code-preview',
    '.chat-message-body',
    '.chat-message-content',
    '.community-post-content',
    '.community-thread-body',
    '.video-description',
    '.profile-bio',
  ].join(',');

  const KEY_TEXT = Object.freeze({
    'app.title': { 'zh-TW': 'hackme_web — 登入系統', en: 'hackme_web — Login System' },
    'tradingWorkflow.title': { 'zh-TW': '交易 Workflow 編輯器', en: 'Trading Workflow Editor' },
    'comfyuiWorkflow.title': { 'zh-TW': 'ComfyUI Workflow 視覺編輯器', en: 'ComfyUI Workflow Visual Builder' },
    'language.label': { 'zh-TW': '語言', en: 'Language' },
    'language.changed': { 'zh-TW': '語言已切換', en: 'Language changed' },
  });

  const EN_TEXT = Object.freeze({
    '語言': 'Language',
    '繁體中文': 'Traditional Chinese',
    '簡單的帳號註冊與登入系統': 'Simple account registration and login',
    '檢查伺服器狀態中': 'Checking server status',
    '登入': 'Log in',
    '註冊': 'Register',
    '帳號': 'Username',
    '密碼': 'Password',
    '請輸入帳號': 'Enter username',
    '請輸入密碼': 'Enter password',
    '顯示密碼': 'Show password',
    '內測 token（內測模式才需要）': 'Internal test token (required only in internal test mode)',
    'root 提供的內測 token': 'Internal test token from root',
    '忘記密碼 / Email 驗證': 'Forgot password / Email verification',
    '帳號或 Email': 'Username or email',
    '送出重設密碼審核': 'Request password reset review',
    '寄送 Email 驗證碼': 'Send email verification code',
    '重設密碼驗證碼': 'Password reset code',
    '請輸入驗證碼': 'Enter verification code',
    '新密碼': 'New password',
    '請輸入新密碼': 'Enter new password',
    '確認新密碼': 'Confirm new password',
    '請再次輸入新密碼': 'Enter the new password again',
    '重設密碼': 'Reset password',
    'Email 驗證碼': 'Email verification code',
    '完成 Email 驗證': 'Complete email verification',
    '選擇一個帳號（3 字以上）': 'Choose a username (at least 3 characters)',
    '請設定密碼': 'Set a password',
    '確認密碼': 'Confirm password',
    '請再次輸入密碼': 'Enter password again',
    '暱稱': 'Display name',
    'Email（選填，用於重設密碼與驗證）': 'Email (optional, for password reset and verification)',
    '真實姓名（選填）': 'Real name (optional)',
    '可留空': 'Optional',
    '生日（選填）': 'Birthday (optional)',
    '電話（選填）': 'Phone (optional)',
    '09xx-xxx-xxx，可留空': '09xx-xxx-xxx, optional',
    '驗證碼': 'Verification code',
    '請輸入答案': 'Enter answer',
    '重新取得': 'Refresh',
    '發佈號: loading...': 'Release: loading...',
    '密碼強度要求：': 'Password strength requirements:',
    '至少 8 個字元': 'At least 8 characters',
    '包含大寫字母（A–Z）': 'Contains uppercase letters (A-Z)',
    '包含小寫字母（a–z）': 'Contains lowercase letters (a-z)',
    '包含符號（!@#$%…）': 'Contains symbols (!@#$%...)',
    '恭喜登入成功': 'Login successful',
    '歡迎回來！': 'Welcome back!',
    '未登入': 'Not signed in',
    '閒置登出：--:--': 'Idle logout: --:--',
    '快速操作': 'Quick actions',
    '修改資料': 'Edit profile',
    '切換淺色模式': 'Switch to light mode',
    '切換夜色模式': 'Switch to night mode',
    '通知': 'Notifications',
    '通知中心': 'Notification Center',
    '全部已讀': 'Mark all read',
    '尚未載入通知': 'Notifications have not loaded yet',
    '回報 Bug': 'Report bug',
    '登出': 'Log out',
    '主要功能': 'Main features',
    '收合側邊欄': 'Collapse sidebar',
    '開啟個人面板': 'Open profile panel',
    '聊天': 'Chat',
    '個人面板': 'Profile',
    '公告': 'Announcements',
    '討論區': 'Forum',
    '雲端硬碟': 'Cloud Drive',
    '相簿': 'Albums',
    '影音': 'Videos',
    '遊戲區': 'Games',
    '實驗區': 'Experiment Lab',
    '純前端 Canvas 教育模擬': 'Frontend-only Canvas education simulations',
    '實驗區目前是純前端 Canvas 教育模擬，沒有後端重型 job、DB 或 worker。': 'The Experiment Lab is currently a frontend-only Canvas education module with no backend heavy job, DB, or worker.',
    '這裡提供低負載、即時互動的視覺化實驗。所有計算都在瀏覽器完成，不會建立後端任務、資料表或背景 worker。': 'This area provides low-load, real-time educational visualizations. All calculations run in the browser, with no backend job, database table, or background worker.',
    '暫停動畫': 'Pause animation',
    '繼續動畫': 'Resume animation',
    '實驗區子分頁': 'Experiment Lab tabs',
    '飛機氣流': 'Airplane airflow',
    '液體分子': 'Liquid molecules',
    '蜂鳥懸停': 'Hummingbird hover',
    '飛機飛行時氣流流動與受力動畫': 'Airflow and force animation for airplane flight',
    'Canvas 動畫區': 'Canvas animation area',
    '控制面板': 'Controls',
    '機翼攻角': 'Angle of attack',
    '襟翼 / 機翼傾角': 'Flap / wing tilt',
    '迎面氣流': 'Headwind',
    '機體速度': 'Aircraft speed',
    '機體地速': 'Aircraft groundspeed',
    '擾流板': 'Spoiler',
    '渦流 / 翼尖擾動': 'Vortex / wingtip disturbance',
    '相對升力': 'Relative lift',
    '相對阻力': 'Relative drag',
    '失速風險': 'Stall risk',
    '觀察重點': 'Observation focus',
    '簡化說明': 'Simplified note',
    '攻角與襟翼增加時，升力會先上升，但阻力與分離風險也會增加；擾流板會降低升力並增加阻力。': 'As angle of attack and flap angle increase, lift rises at first, but drag and flow-separation risk also increase; spoilers reduce lift and add drag.',
    '攻角與襟翼增加時，升力會先上升，但過高攻角會進入失速區，升力下降且阻力快速增加；機體地速與迎面氣流共同形成相對空速。': 'As angle of attack and flap angle increase, lift rises at first, but excessive angle of attack enters the stall region, reducing lift and rapidly increasing drag; aircraft groundspeed and headwind combine into relative airspeed.',
    '這是教育近似，不是 CFD。動畫只用簡化粒子與相對量呈現氣流、升力、阻力與失速趨勢。': 'This is an educational approximation, not CFD. The animation uses simplified particles and relative values to show airflow, lift, drag, and stall tendencies.',
    '液體分子移動動畫': 'Liquid molecule motion animation',
    '杯子傾角': 'Cup tilt',
    '黏滯度': 'Viscosity',
    '液面高度': 'Liquid level',
    '搖動杯子': 'Shake cup',
    '傾倒杯子': 'Pour cup',
    '丟入物品': 'Drop object',
    '重置液體': 'Reset liquid',
    '分子動能': 'Molecular kinetic energy',
    '可見粒子數': 'Visible particles',
    '杯中物品數': 'Objects in cup',
    '黏滯度越低，粒子受到搖動與傾倒後會更快流動；液面越高，杯中粒子可活動區域越大。': 'Lower viscosity lets particles flow faster after shaking or pouring; a higher liquid level gives the particles more room to move inside the cup.',
    '黏滯度越低，粒子受到搖動與傾倒後會更快流動；杯子傾斜時液面仍以重力方向為準，傾角過大會真的倒出可見粒子。': 'Lower viscosity lets particles flow faster after shaking or pouring; when the cup tilts, the liquid surface still follows gravity, and excessive tilt actually pours out visible particles.',
    '這是即時粒子近似，不是嚴格 Navier-Stokes / SPH。碰撞、表面張力與邊界條件都以低 CPU 的方式簡化。': 'This is a real-time particle approximation, not strict Navier-Stokes or SPH. Collisions, surface tension, and boundary conditions are simplified for low CPU use.',
    '蜂鳥在空中懸停時的氣流走向': 'Airflow direction while a hummingbird hovers',
    '蜂鳥懸停時的下洗氣流動畫': 'Downwash animation for hummingbird hover',
    '翼拍頻率': 'Wingbeat frequency',
    '翼面振幅': 'Wing amplitude',
    '懸停穩定度': 'Hover stability',
    '顯示蜂鳥腦中需估計的控制量': 'Show control quantities estimated by the hummingbird brain',
    '下洗強度': 'Downwash strength',
    '左右平衡': 'Left/right balance',
    '翼拍週期': 'Wingbeat cycle',
    '蜂鳥腦中需計算 / 估計': 'What the hummingbird brain must calculate / estimate',
    '身體姿態誤差': 'Body posture error',
    '視覺與前庭回授': 'Visual and vestibular feedback',
    '花朵位置、距離、風造成的漂移': 'Flower position, distance, and wind drift',
    '兩翼翼拍頻率、相位、振幅與左右不對稱修正': 'Wingbeat frequency, phase, amplitude, and left/right asymmetry correction',
    '升力是否足以抵消體重': 'Whether lift is enough to offset body weight',
    '側風、花朵晃動、肌肉延遲補償': 'Crosswind, flower motion, and muscle-delay compensation',
    '能量成本與疲勞取捨': 'Energy cost and fatigue tradeoff',
    '這是教育視覺化，只顯示翼拍、平衡修正與下洗氣流的近似關係，不代表真實神經控制或流體工程模型。': 'This is an educational visualization showing approximate relationships between wingbeat, balance correction, and downwash. It is not a real neural-control or fluid-engineering model.',
    '這是教育視覺化，只顯示翼拍、平衡修正與下洗氣流的近似關係，不代表真實神經控制或流體工程模型。翼拍動畫採慢動作呈現，KPI 保留真實頻率與週期。': 'This is an educational visualization showing approximate relationships between wingbeat, balance correction, and downwash. It is not a real neural-control or fluid-engineering model. Wingbeat animation is shown in slow motion, while KPIs keep the real frequency and cycle.',
    'AI 產圖': 'AI Images',
    '積分系統': 'Points',
    '積分交易所': 'Points Exchange',
    '申覆': 'Appeals',
    '任務中心': 'Job Center',
    '分享管理': 'Share Management',
    '帳號管理': 'Account Management',
    '安全中心': 'Security Center',
    '日常': 'Daily',
    '社群': 'Community',
    '工具': 'Tools',
    '管理': 'Management',
    '我的主頁': 'My Profile',
    '編輯資料': 'Edit Profile',
    '好友': 'Friends',
    '重新整理': 'Refresh',
    '重新整理聊天室': 'Refresh rooms',
    '建立聊天室': 'Create room',
    '加入聊天室': 'Join room',
    '快速私聊對象': 'Quick DM target',
    '帳號，可留空': 'Username, optional',
    '聊天室名稱': 'Room name',
    '最多 48 字': 'Up to 48 characters',
    '建立時加入成員': 'Add members on create',
    '帳號，逗號分隔': 'Usernames, comma-separated',
    '加入密碼': 'Join password',
    '允許匿名': 'Allow anonymous',
    '我在此群匿名': 'Join this group anonymously',
    '關閉': 'Close',
    '取消': 'Cancel',
    '聊天室 ID': 'Room ID',
    '例如 12': 'Example: 12',
    '聊天室密碼': 'Room password',
    '若有密碼才需填寫': 'Required only if the room has a password',
    '若允許，以匿名加入': 'Join anonymously if allowed',
    '好友帳號': 'Friend username',
    '加好友': 'Add friend',
    '邀請成員（帳號，逗號分隔）': 'Invite members (usernames, comma-separated)',
    '邀請': 'Invite',
    '備份聊天記錄': 'Export chat history',
    '輸入訊息，最多 500 字': 'Type a message, up to 500 characters',
    '送出': 'Send',
    '上傳附件': 'Upload attachment',
    '微笑': 'Smile',
    '感謝': 'Thanks',
    '了解': 'Got it',
    '驚訝': 'Surprised',
    '加油': 'Cheer',
    '難過': 'Sad',
    '重新整理訊息': 'Refresh messages',
    '好友代碼': 'Friend code',
    '複製好友代碼': 'Copy friend code',
    '重新產生': 'Regenerate',
    '儲存資料': 'Save profile',
    '帳號設定': 'Account settings',
    '更新頭像': 'Update avatar',
    '頭像圖片（JPEG / PNG / GIF）': 'Avatar image (JPEG / PNG / GIF)',
    '或從雲端硬碟選擇圖片': 'Or choose an image from Cloud Drive',
    '讀取雲端圖片中...': 'Loading cloud images...',
    '刷新': 'Refresh',
    '使用': 'Use',
    '僅列出你自己的雲端硬碟中可作為公開頭像的 JPEG / PNG / GIF 圖片。': 'Only your own Cloud Drive JPEG / PNG / GIF images that can be used as a public avatar are listed.',
    '個人外觀': 'Personal appearance',
    '外觀模板': 'Appearance template',
    '快速色系': 'Quick color mode',
    '夜色': 'Night',
    '淺色': 'Light',
    '自訂色盤': 'Custom palette',
    '保留自訂色盤': 'Keep custom palette',
    '加入好友': 'Add friend',
    '好友 username 或 user id': 'Friend username or user ID',
    '送出申請': 'Send request',
    '收到的申請': 'Incoming requests',
    '送出的申請': 'Sent requests',
    '分享連結': 'Share links',
    '我的影音': 'My videos',
    '發布公告': 'Publish announcement',
    '公告標題': 'Announcement title',
    '公告內容': 'Announcement content',
    '輸入公告內容': 'Enter announcement content',
    '置頂公告': 'Pin announcement',
    '公告 ID': 'Announcement ID',
    '上傳新附件': 'Upload new attachment',
    '選擇雲端檔案': 'Choose cloud file',
    '請先選擇雲端檔案': 'Select a cloud file first',
    '理由': 'Reason',
    '附件用途': 'Attachment purpose',
    '上傳並送審': 'Upload and submit for review',
    '既有檔送審': 'Submit existing file for review',
    '工具': 'Tools',
    '審核': 'Review',
    '申請建立討論區': 'Request forum board',
    '管理分類': 'Manage categories',
    '收起': 'Collapse',
    '討論分類': 'Forum category',
    '討論區名稱': 'Board name',
    '例如：遊戲交流區': 'Example: Game discussion',
    '討論區說明': 'Board description',
    '說明討論主題、規則與用途': 'Describe topics, rules, and purpose',
    '版規': 'Board rules',
    '例如：禁止人身攻擊、禁止外流個資、禁止洗版': 'Example: No personal attacks, doxxing, or spam',
    '可見性': 'Visibility',
    '排序': 'Sort order',
    '新增分類': 'Add category',
    '分類名稱': 'Category name',
    '例如：公告討論、技術交流': 'Example: Announcements, tech talk',
    '分類說明': 'Category description',
    '分類用途說明': 'Describe this category',
    '搜尋討論區': 'Search boards',
    '搜尋名稱、說明、版主': 'Search name, description, moderators',
    '返回討論區': 'Back to boards',
    '撰寫新主題': 'New thread',
    '版主設定': 'Moderator settings',
    '選擇版主帳號': 'Select moderator account',
    '常用權限組': 'Permission preset',
    '審核主題': 'Review threads',
    '置頂主題': 'Pin thread',
    '置頂留言': 'Pin replies',
    '鎖定主題': 'Lock thread',
    '編輯留言': 'Edit replies',
    '刪除主題或留言': 'Delete threads or replies',
    '獎勵作者': 'Reward authors',
    '懲處違規留言': 'Penalize violating replies',
    '新增 / 更新版主': 'Add / update moderator',
    '新主題標題': 'New thread title',
    '輸入主題標題': 'Enter thread title',
    '內容': 'Content',
    '輸入主題內容': 'Enter thread content',
    '發布主題': 'Publish thread',
    '搜尋主題、內容、作者': 'Search threads, content, authors',
    '上一頁': 'Previous',
    '下一頁': 'Next',
    '返回主題列表': 'Back to thread list',
    '刪除主題': 'Delete thread',
    '留言回覆': 'Reply',
    '輸入回覆內容': 'Enter reply',
    '送出留言': 'Send reply',
    '刷新申覆': 'Refresh appeals',
    '雲端硬碟分頁': 'Cloud Drive tabs',
    '檔案管理': 'Files',
    '容量管理': 'Capacity',
    '雲端硬碟容量使用率': 'Cloud Drive capacity usage',
    '雲端硬碟用量': 'Cloud Drive usage',
    '選購方案': 'Choose plan',
    '重新整理雲端硬碟狀態': 'Refresh Cloud Drive status',
    '選擇 .torrent': 'Choose .torrent',
    '+ 資料夾': '+ Folder',
    '新增文檔': 'New document',
    '上傳檔案': 'Upload file',
    '上傳資料夾': 'Upload folder',
    'BT / .torrent': 'BT / .torrent',
    '移動資料夾': 'Move folder',
    '全部還原': 'Restore all',
    '清空': 'Clear',
    '預覽': 'Preview',
    '刷新': 'Refresh',
    '智慧整理方式': 'Smart organization mode',
    '智慧整理': 'Smart organize',
    '相簿名稱': 'Album name',
    '我的相簿': 'My album',
    '描述': 'Description',
    '旅行、專案、公開素材...': 'Travel, projects, public assets...',
    '分享密碼（選填）': 'Share password (optional)',
    '持連結可看時生效': 'Applies when link access is enabled',
    '建立': 'Create',
    '分享密碼': 'Share password',
    '留空不變，輸入新密碼可更新': 'Leave blank to keep current, enter a new password to update',
    '儲存': 'Save',
    '預覽圖大小': 'Thumbnail size',
    '已完成複製': 'Copied',
    '連結已複製': 'Link copied',
    '請先登入': 'Please log in first',
    '載入中': 'Loading',
    '讀取中': 'Loading',
    '同步中': 'Syncing',
    '處理中': 'Processing',
    '準備中': 'Preparing',
    '完成': 'Done',
    '失敗': 'Failed',
    '成功': 'Success',
    '錯誤': 'Error',
    '警告': 'Warning',
    '正常': 'Normal',
    '啟用': 'Enabled',
    '停用': 'Disabled',
    '開啟': 'Open',
    '編輯': 'Edit',
    '刪除': 'Delete',
    '查看': 'View',
    '複製': 'Copy',
    '更新': 'Update',
    '新增': 'Add',
    '搜尋': 'Search',
    '狀態': 'Status',
    '設定': 'Settings',
    '原因': 'Reason',
    '說明': 'Description',
    '名稱': 'Name',
    '角色': 'Role',
    '等級': 'Level',
    '權限': 'Permissions',
    'hackme_web — 登入系統': 'hackme_web - Login System',
    '登入系統': 'Login System',
    '測試 / 內測 token（可取代密碼）': 'Test / internal test token (can replace password)',
    '積分': 'Points',
    '違規扣點': 'Violation deductions',
    '權限等級': 'Access level',
    '連線檢查中': 'Checking connection',
    '即時聊天室': 'Live chat',
    '我的聊天室': 'My chat rooms',
    '請先建立或加入聊天室': 'Create or join a room first',
    '從雲端檔案附加…': 'Attach from Cloud Drive...',
    '選檔後會立即加入待送附件，可送出前先移除。': 'Selected files are added as pending attachments immediately and can be removed before sending.',
    '尚未選擇要隨訊息送出的附件': 'No attachments selected for this message',
    '聊天室共用附件': 'Room shared attachments',
    '管理自己的主頁、資料與好友。': 'Manage your profile, information, and friends.',
    '這個人很懶什麼都沒寫': 'This person is too lazy to write anything.',
    '好友狀態': 'Friend status',
    '本人': 'You',
    '主頁可見性': 'Profile visibility',
    '我的好友代碼': 'My friend code',
    '顯示名稱': 'Display name',
    '所在地': 'Location',
    '網站': 'Website',
    '公開': 'Public',
    '私人': 'Private',
    '個人簡介': 'Bio',
    '簽名': 'Signature',
    '用好友代碼加入': 'Add by friend code',
    '送出好友申請': 'Send friend request',
    '指定對象清單：一般用戶限好友；root/admin 管理用途可見全站用戶，好友會排在前列並標註。': 'Target selection: regular users can choose friends only; root/admin can see all users for management use, with friends pinned and labeled first.',
    '集中查看 ComfyUI、影音轉碼、報告、檢查與其他長時間任務。': 'View ComfyUI, media transcoding, reports, checks, and other long-running jobs in one place.',
    '進行中': 'In progress',
    '可檢查錯誤階段': 'Error stage can be inspected',
    '最近任務': 'Recent jobs',
    '管理檔案分享連結與自己上傳的影音。': 'Manage file share links and your uploaded media.',
    '啟用中': 'Active',
    '仍可存取': 'Still accessible',
    '已結束': 'Ended',
    '到期 / 次數用完 / 撤銷': 'Expired / exhausted / revoked',
    '總存取': 'Total access',
    '所有分享累計': 'All shares combined',
    '影片': 'Videos',
    '自己的上傳': 'Your uploads',
    '觀看': 'Views',
    '累計觀看': 'Total views',
    '按讚': 'Likes',
    '累計按讚': 'Total likes',
    '收益': 'Revenue',
    '創作者實收': 'Creator net',
    '平台分潤': 'Platform share',
    '投幣手續費': 'Tip fee',
    '曝光': 'Promotion',
    '已投入積分': 'Points spent',
    '最新公告': 'Latest announcements',
    '公告附件請求（需 root 核准）': 'Announcement attachment requests (root approval required)',
    '公告附件視為管理文件，送審後需 root 核准才會對公告生效。': 'Announcement attachments are treated as administrative files and require root approval before taking effect.',
    '討論區列表': 'Board list',
    '討論區工具': 'Forum tools',
    '不列入公開列表': 'Do not list publicly',
    '不列出，持連結可看': 'Unlisted, accessible by link',
    '討論分類管理': 'Forum category management',
    '待審核討論區': 'Boards pending review',
    '待審核主題': 'Threads pending review',
    '請先選擇討論區': 'Select a board first',
    '版主與權限範圍': 'Moderators and permission scope',
    '讀取會員中...': 'Loading members...',
    '完整版主': 'Full moderator',
    '只審核主題': 'Thread review only',
    '內容整理': 'Content curation',
    '懲處與刪除': 'Penalties and deletion',
    '第 1 頁': 'Page 1',
    '主題內容': 'Thread content',
    '違規申覆': 'Violation appeals',
    '容量使用狀態': 'Capacity usage status',
    '容量使用率': 'Capacity usage',
    '剩餘容量': 'Remaining capacity',
    '單檔限制': 'Single-file limit',
    '每日上傳': 'Daily upload',
    '安全措施': 'Security measures',
    '檔案狀態統計': 'File status statistics',
    '隱私模式': 'Privacy mode',
    '三種模式怎麼選': 'How to choose among the three modes',
    '一般檔案（可掃毒、可預覽、可分享）': 'Regular file (scan, preview, and share supported)',
    '伺服器端加密（磁碟密文、下載明文）': 'Server-side encryption (encrypted at rest, decrypted on download)',
    '端到端加密（站方無法讀取）': 'End-to-end encryption (site cannot read it)',
    '一般檔案（可掃毒、可預覽）': 'Regular file (scan and preview supported)',
    '非 E2EE 會讓伺服器取得明文以便掃毒與預覽；E2EE 需要自己保管檔案加密密碼，忘記後站方無法救回。': 'Non-E2EE uploads let the server access plaintext for virus scanning and previews; E2EE requires you to keep the file password yourself, and the site cannot recover it if lost.',
    '資料夾與檔案': 'Folders and files',
    '上傳、下載、整理與刪除都在這裡處理。': 'Upload, download, organize, and delete files here.',
    '上傳會放進目前資料夾；Direct link 永遠可用，BT 功能會自動檢查伺服器能力。': 'Uploads go into the current folder; direct links are always available, and BT checks server capability automatically.',
    '已刪除檔案': 'Deleted files',
    '還原或永久移除已刪除檔案。': 'Restore or permanently remove deleted files.',
    '檔案預覽': 'File preview',
    '點一次在這裡預覽；連點同一檔案兩次開全頁預覽。': 'Click once to preview here; click the same file twice to open the full-page preview.',
    '請從左側檔案清單選擇要預覽的檔案': 'Select a file from the left file list to preview it',
    '相簿管理': 'Album management',
    '建立、編輯與瀏覽相簿；圖片可在雲端硬碟檔案列表直接加入。': 'Create, edit, and browse albums; images can be added directly from the Cloud Drive file list.',
    '依資料夾': 'By folder',
    '依上傳月份': 'By upload month',
    '依圖片 / 影片': 'By image / video',
    '全部媒體一本': 'All media in one album',
    '相簿內容': 'Album contents',
    '目前未設定分享密碼。': 'No share password is currently set.',
    '清除分享密碼': 'Clear share password',
    '相簿預覽': 'Album preview',
    '預設開啟第一本相簿，圖片直接顯示預覽圖。': 'The first album opens by default and images are shown as thumbnails.',
    '小': 'Small',
    '中': 'Medium',
    '大': 'Large',
    '從自己的雲端硬碟發布影片或音樂，其他人可觀看 / 收聽、按讚、留言與投幣。': 'Publish videos or music from your Cloud Drive so others can watch/listen, like, comment, and tip.',
    '發布影音': 'Publish media',
    '清除': 'Clear',
    '一般檔案': 'Regular file',
    '伺服器端加密': 'Server-side encryption',
    '標題': 'Title',
    '發布': 'Publish',
    '可直接上傳影音，或選擇雲端硬碟影音發布；只會使用 cloud_file_id 串接，不公開 storage path。': 'Upload media directly or publish existing Cloud Drive media; only cloud_file_id is used, and storage paths are not exposed.',
    '直接上傳影音': 'Direct media upload',
    '可直接發布影片或音樂；系統仍會存入你的 Cloud Drive，不建立第二套檔案系統。公開 / 持連結可看影片與伺服器端加密影音會優先嘗試建立 HLS 串流衍生檔；轉檔期間可以先做別的事，進度會顯示在任務中心，完成後會通知上傳者。': 'Publish video or music directly; the system still stores it in your Cloud Drive and does not create a second file system. Public/link-access videos and server-side encrypted media will prefer HLS derivatives. You can do other work during transcoding; progress appears in Job Center and the uploader is notified when it finishes.',
    '封面圖（選填）': 'Cover image (optional)',
    '建議使用 16:9 圖片；不提供時影片首頁會使用影片首幀或音樂預設封面。': 'A 16:9 image is recommended; if omitted, the video first frame or default music cover is used.',
    '上傳隱私': 'Upload privacy',
    '雲端硬碟影音': 'Cloud Drive media',
    '不選直接上傳影音時，才使用既有雲端影音發布。': 'Used only when direct media upload is not selected.',
    '持連結可看且未設定時，只要拿到完整分享連結即可觀看；若是 E2EE 分享影音，完整連結仍會包含只留在瀏覽器端的片段金鑰。': 'When link access is enabled without a password, anyone with the full link can watch; for E2EE shared media, the full link still contains the browser-only fragment key.',
    '分享有效到（選填）': 'Share valid until (optional)',
    '超過時間後分享連結會失效。': 'The share link expires after this time.',
    '到期時間': 'Expiration time',
    '到期時間（選填）': 'Expiration time (optional)',
    '日期': 'Date',
    '時間': 'Time',
    '用日曆選擇日期；只選日期時預設當天 23:59 失效。': 'Choose a date from the calendar; if only a date is selected, it expires at 23:59 that day.',
    '最大觀看次數（選填）': 'Maximum views (optional)',
    '分享頁首次成功載入會計一次；0 代表不限。': 'The first successful share-page load counts once; 0 means unlimited.',
    'E2EE 影音對外觀看會改用「持連結可看」與片段金鑰授權；觀看者使用完整分享連結即可在瀏覽器端解密，不需要知道原始 E2EE 密碼。若遺失完整分享連結中的片段金鑰，伺服器無法復原，只能重新產生分享。': 'E2EE media is shared externally through link access and fragment-key authorization. Viewers can decrypt in the browser with the complete share link and do not need the original E2EE password. If the fragment key in the complete share link is lost, the server cannot recover it and the share must be regenerated.',
    '影片彈幕控制': 'Video danmaku controls',
    '彈幕開': 'Danmaku on',
    '彈幕關': 'Danmaku off',
    '密度': 'Density',
    '透明度': 'Opacity',
    '模式': 'Mode',
    '滾動': 'Scroll',
    '頂部': 'Top',
    '底部': 'Bottom',
    '顏色': 'Color',
    '在目前時間點送出彈幕': 'Send danmaku at the current time',
    '送出彈幕': 'Send danmaku',
    '彈幕會綁定目前播放時間，最多 80 字。': 'Danmaku is bound to the current playback time, up to 80 characters.',
    '彈幕已送出': 'Danmaku sent.',
    '彈幕載入失敗': 'Failed to load danmaku.',
    '彈幕送出失敗': 'Failed to send danmaku.',
    '準備上傳': 'Ready to upload',
    '最新': 'Latest',
    '熱門': 'Popular',
    '趨勢': 'Trending',
    '目前開放西洋棋、中國象棋、數獨、踩地雷、1A2B、俄羅斯方塊、真實版俄羅斯方塊、宇宙戰機、3D 射擊場、彈幕遊戲、火柴人橫向射擊、貪食蛇、2048、打磚塊、黑白棋、圍棋與五子棋。3D 與火柴人支援多人邀請；使用下拉選單切換遊戲。': 'Available games include chess, Chinese chess, Sudoku, Minesweeper, 1A2B, Tetris, real Tetris, Space Shooter, 3D Shooting Range, bullet-hell, stickman side-scroller shooter, Snake, 2048, Breakout, Reversi, Go, and Gomoku. 3D and stickman games support multiplayer invitations; use the dropdown to switch games.',
    '遊戲大廳': 'Game lobby',
    '多人房間': 'Multiplayer rooms',
    '邀請另一位玩家加入合作或對戰。': 'Invite another player for co-op or versus play.',
    '邀請玩家': 'Invite player',
    '載入玩家中...': 'Loading players...',
    '合作破關': 'Co-op',
    '邀請加入': 'Invite to join',
    '電腦練習方位': 'Computer practice side',
    '我執白方': 'I play white',
    '我執黑方': 'I play black',
    '電腦難度': 'Computer difficulty',
    '模式': 'Mode',
    '實驗 0：2 層物質 minimax': 'Experiment 0: depth-2 material minimax',
    '實驗 1：引擎搜尋 + 對局學習': 'Experiment 1: engine search + game learning',
    '實驗 2：NN 評估': 'Experiment 2: NN evaluation',
    '實驗 3：DL 語義平衡學習': 'Experiment 3: DL semantic balance learning',
    '實驗 4：Policy/Value + MCTS': 'Experiment 4: Policy/Value + MCTS',
    '實驗 5：NNUE + AlphaBeta/PVS': 'Experiment 5: NNUE + AlphaBeta/PVS',
    '實驗 6：Neural Network': 'Experiment 6: Neural Network',
    '實驗 3：DL 語義平衡': 'Experiment 3: DL semantic balance',
    'Stockfish 深度': 'Stockfish depth',
    '深度越高越強也越耗 CPU；每局保存，限制 1-20。': 'Higher depth is stronger and uses more CPU; saved per game, limited to 1-20.',
    '和電腦練習': 'Practice against computer',
    '對戰邀請': 'Match invitations',
    '我的棋局': 'My games',
    '全螢幕遊戲': 'Fullscreen game',
    '尚未選擇棋局': 'No game selected',
    '選擇棋局後即可走棋': 'Select a game to start moving',
    '求和': 'Offer draw',
    '接受和棋': 'Accept draw',
    '拒絕和棋': 'Reject draw',
    '申請和棋': 'Request draw',
    '認輸': 'Resign',
    '賽後分析': 'Post-game analysis',
    '殘局題': 'Endgame puzzles',
    '啟用競賽計時': 'Enable match timer',
    '提示': 'Hint',
    '自訂': 'Custom',
    '白 --:--': 'White --:--',
    '黑 --:--': 'Black --:--',
    '西洋棋': 'Chess',
    '中國象棋': 'Chinese chess',
    '玩法說明': 'How to play',
    '西洋棋玩法說明': 'How to play chess',
    '白方先走，雙方輪流移動一枚棋子。所有移動必須符合西洋棋規則，不能讓自己的王處於被將軍狀態。': 'White moves first, and players alternate moving one piece. Every move must follow chess rules and may not leave your king in check.',
    '特殊走法：王車易位先點王，再點亮起的 g/c 目標格；也可點王後直接點同側車。兵走到底會升變，預設升后，可選 q/r/b/n。吃過路兵合法時目標格會亮起。': 'Special moves: for castling, click the king first and then the highlighted g/c target square, or click the king and then the rook on that side. Pawns promote on the last rank, defaulting to queen; q/r/b/n can be selected. Legal en-passant target squares are highlighted.',
    '目標是將死對方的王。可和玩家對戰，也可選擇白方/黑方與電腦難度練習。': 'The goal is to checkmate the opponent king. You can play another player or practice against the computer as white or black at a selected difficulty.',
    '數獨玩法說明': 'How to play Sudoku',
    '按「開始」後才會出現題目並開始計時。每列、每欄、每個 3x3 宮格都必須填入 1 到 9，且不可重複。': 'The puzzle appears and the timer starts only after pressing Start. Each row, column, and 3x3 box must contain 1 through 9 without duplicates.',
    '按「檢查」若仍有錯誤會加時 10 秒；每局提示最多 3 次，每次提示會填入 1 格並加時 60 秒；正確完成後依完成時間排行。': 'Press Check to validate. If errors remain, 10 seconds are added. Each game allows up to 3 hints; each hint fills one cell and adds 60 seconds. Completed games are ranked by finish time.',
    '按開始後才會出現題目並開始計時。': 'The puzzle appears and the timer starts after pressing Start.',
    '踩地雷玩法說明': 'How to play Minesweeper',
    '按「開始」後才會產生盤面並開始計時。左鍵翻開格子，右鍵插旗標記地雷。': 'The board is generated and the timer starts after pressing Start. Left-click to reveal a cell; right-click to flag a mine.',
    '數字代表周圍八格的地雷數。翻開所有非地雷格即獲勝；踩到地雷本局結束。排行依完成時間計算。': 'Numbers show how many mines surround that cell. Reveal all non-mine cells to win; hitting a mine ends the game. Ranking is based on completion time.',
    '按開始後才會出現盤面並開始計時。': 'The board appears and the timer starts after pressing Start.',
    '1A2B 玩法說明': 'How to play 1A2B',
    '按「開始」後系統會產生 4 位不重複數字，首位不會是 0。每次猜測也必須是 4 位不重複數字，且首位不可為 0，例如 1234。': 'After pressing Start, the system generates a 4-digit number with no repeated digits and no leading zero. Each guess must also be 4 unique digits with no leading zero, such as 1234.',
    'A 代表數字與位置都正確，B 代表數字正確但位置不同。排行先比猜測次數，再比總猜測時間；超過 5 分鐘不列入排名。': 'A means both digit and position are correct; B means the digit is correct but in a different position. Ranking compares guess count first, then total time; games over 5 minutes are excluded.',
    '按開始後才會產生答案並開始計時。': 'The answer is generated and the timer starts after pressing Start.',
    '俄羅斯方塊玩法說明': 'How to play Tetris',
    '方向鍵左右移動、向下加速、向上旋轉，C 可 Hold 保留方塊，空白鍵可直接落下。填滿一整行即可消除並得分，連續消行會增加 Combo 加分。': 'Use arrow keys to move left/right, down to soft drop, and up to rotate. C holds a piece, and Space hard-drops it. Fill a full row to clear it and score; consecutive clears add combo bonuses.',
    '排行榜以本週最高分排序，分數相同時完成時間較短者在前。': 'The leaderboard is sorted by this week highest score; ties are ordered by shorter completion time.',
    '按開始後開始落方塊，最高分列入排行榜。': 'Pieces start falling after pressing Start, and the highest score enters the leaderboard.',
    '宇宙戰機玩法說明': 'How to play Space Shooter',
    '方向鍵或 A/D 左右移動，空白鍵發射。擊落敵機可得分並可能掉落升級，Boss 會發射子彈，敵機穿越底線或撞到玩家會扣生命。': 'Use arrow keys or A/D to move left/right, and Space to fire. Destroy enemies to score and possibly drop upgrades. Bosses fire bullets; enemies crossing the bottom or colliding with you cost lives.',
    '生命歸零時結算，排行榜以本週最高分排序。': 'The game ends when lives reach zero. The leaderboard is sorted by this week highest score.',
    '按開始後出擊，最高分列入排行榜。': 'Launch after pressing Start, and the highest score enters the leaderboard.',
    '3D 射擊場玩法說明': 'How to play 3D Shooting Range',
    '選擇模式後按「開始」。滑鼠拖曳或滑鼠鎖定後移動視角，WASD 移動，左鍵或空白鍵射擊，E 可嘗試拆彈。瞄準鏡會有輕微呼吸晃動。': 'Choose a mode and press Start. Drag the mouse or use pointer lock to look around, move with WASD, shoot with left-click or Space, and press E to attempt bomb defusal. The scope has slight breathing sway.',
    '模式包含靶場、PvE、拆彈、Bot Match、Co-op PvE、PvP Duel 與 Battle Royale。關卡會逐步開出新地圖、Boss 橋段、敵人 AI、武器與地面物資。': 'Modes include range, PvE, defusal, Bot Match, Co-op PvE, PvP Duel, and Battle Royale. Levels gradually unlock maps, boss segments, enemy AI, weapons, and ground loot.',
    '選擇模式後開始，最高分列入排行榜。': 'Choose a mode to start; the highest score enters the leaderboard.',
    '開始': 'Start',
    '檢查': 'Check',
    '筆記': 'Notes',
    '簡單 9x9 / 10 雷': 'Easy 9x9 / 10 mines',
    '普通 12x12 / 20 雷': 'Normal 12x12 / 20 mines',
    '困難 16x16 / 40 雷': 'Hard 16x16 / 40 mines',
    '大師 18x18 / 70 雷': 'Master 18x18 / 70 mines',
    '插旗模式': 'Flag mode',
    '安全提示': 'Safe hint',
    '限時關': 'Timed mode',
    '猜測數字': 'Guess number',
    '結束': 'End',
    '衝刺': 'Sprint',
    '蹲下': 'Crouch',
    '匍匐': 'Prone',
    '換彈': 'Reload',
    '武器': 'Weapon',
    '射擊': 'Shoot',
    '遊戲': 'Game',
    '3D 射擊場': '3D shooting range',
    '第 1 關 貨櫃倉庫': 'Level 1 Container Warehouse',
    '第 2 關 反應爐中庭': 'Level 2 Reactor Courtyard',
    '第 3 關 地鐵月台': 'Level 3 Metro Platform',
    '第 4 關 核心堡壘': 'Level 4 Core Fortress',
    '數獨': 'Sudoku',
    '踩地雷': 'Minesweeper',
    '俄羅斯方塊': 'Tetris',
    '宇宙戰機': 'Space shooter',
    '全螢幕': 'Fullscreen',
    '尚未開始': 'Not started',
    '前': 'Forward',
    '後': 'Back',
    '左': 'Left',
    '右': 'Right',
    '下': 'Down',
    '旋轉': 'Rotate',
    '落下': 'Drop',
    '暫停': 'Pause',
    '發射': 'Fire',
    '線上遊戲': 'Online game',
    '今日挑戰': 'Daily challenge',
    '每日排行榜': 'Daily leaderboard',
    '週排行榜': 'Weekly leaderboard',
    '每日任務完成後可領積分，並使用同一題目/種子排行。': 'Complete the daily mission to claim points; rankings use the same puzzle/seed.',
    '只比較今日挑戰成績': 'Only today challenge scores are compared',
    '週末前三名發放 300 / 200 / 100 積分': 'Top 3 at week end receive 300 / 200 / 100 points',
    '正式環境只做 warm start；user game 只累積 replay，不直接改 production model。': 'Production only performs warm start; user games accumulate replay data and do not directly modify the production model.',
    '發放週獎勵': 'Issue weekly rewards',
    '成就與回放分享': 'Achievements and replay sharing',
    '本機成就': 'Local achievements',
    '最近回放摘要': 'Recent replay summary',
    '刷新引擎狀態': 'Refresh engine status',
    '尚未讀取 chess engine dashboard。': 'Chess engine dashboard has not been loaded yet.',
    '生成': 'Generate',
    '歷史重跑': 'History rerun',
    'Civitai / 模型管理': 'Civitai / model management',
    'Civitai / 模型匯入': 'Civitai / model import',
    '模型管理': 'Model management',
    '模式讀取中': 'Loading mode',
    '目前模式：讀取中': 'Current mode: loading',
    '正在確認目前是本地模式還是雲端 / 遠端模式。': 'Checking whether the current mode is local, cloud, or remote.',
    '尚未連線': 'Not connected',
    '重新整理模型': 'Refresh models',
    '載入上次設定': 'Load last settings',
    '匯入 workflow 模板檔（JSON）': 'Import workflow template file (JSON)',
    '啟動 ComfyUI': 'Start ComfyUI',
    '停止 ComfyUI': 'Stop ComfyUI',
    'Workflow 模板': 'Workflow templates',
    '先選擇模板': 'Choose a template first',
    '可從官方模板、自己的 preset、其他公開 preset，或先匯入模板檔後再建立簡化卡片頁面。': 'Choose from official templates, your own presets, other public presets, or import a template file before creating a simplified card page.',
    '手動欄位': 'Manual fields',
    '模板卡片以外的原始欄位。只有在需要直接手動調整時才展開。': 'Raw fields outside the template card. Expand this only when direct manual adjustment is needed.',
    '檢查模型': 'Check model',
    '檢查後選擇精度版本': 'Choose precision after checking',
    'GGUF 是單一量化 component，Diffusers 需搭配 base repo 的 tokenizer、scheduler、VAE 等組件。': 'GGUF is a single quantized component; Diffusers must be paired with base-repo components such as tokenizer, scheduler, and VAE.',
    '貼上 repo 後會先檢查支援模式與可下載精度版本。': 'After pasting a repo, supported modes and downloadable precision versions are checked first.',
    'Diffusers 模式可在這裡輸入 namespace/model 簡寫或 Hugging Face 模型頁網址；此欄會覆蓋 root 的預設 repo。': 'In Diffusers mode, enter a namespace/model shorthand or Hugging Face model URL here; this overrides the root default repo.',
    '模型': 'Model',
    '讀取模型中...': 'Loading models...',
    '模型族群讀取中。': 'Loading model families.',
    '使用各自大模型內建 VAE': "Use each model's built-in VAE",
    '留白代表使用各自大模型內建 VAE；也可改用已安裝的獨立 VAE 檔。': 'Blank means each model uses its built-in VAE; you can also switch to an installed standalone VAE file.',
    '生成模式': 'Generation mode',
    '文字生圖': 'Text to image',
    '圖生圖': 'Image to image',
    '局部重繪': 'Inpaint',
    '向外延展': 'Outpaint',
    '放大修復': 'Upscale repair',
    '文字生影片': 'Text to video',
    '圖生影片': 'Image to video',
    '影片生影片': 'Video to video',
    '文字轉語音': 'Text to speech',
    '文字生成語音影片': 'Text to speech video',
    '重繪強度': 'Denoise strength',
    '圖生圖、局部重繪、向外延展時使用，越高代表越偏離原圖。': 'Used for image-to-image, inpaint, and outpaint; higher values diverge more from the original image.',
    '放大模型': 'Upscale model',
    '讀取放大模型中...': 'Loading upscale models...',
    '來源圖片': 'Source image',
    '文字生圖：只需要提示詞，不需要來源圖片。': 'Text-to-image only needs a prompt and does not require a source image.',
    'img2img、局部重繪、向外延展、放大修復會使用這張圖。': 'img2img, inpaint, outpaint, and upscale repair use this image.',
    '可上傳 PNG、JPG、WEBP。': 'PNG, JPG, and WEBP can be uploaded.',
    '尚未選擇來源圖片': 'No source image selected',
    '選擇既有圖片': 'Choose existing image',
    '清除來源圖': 'Clear source image',
    '遮罩圖片': 'Mask image',
    '局部重繪使用遮罩；白色 / 不透明區域代表要重畫的位置。': 'Inpaint uses a mask; white / opaque areas indicate where to redraw.',
    '建議與來源圖片尺寸一致。': 'Recommended to match the source image size.',
    '尚未選擇遮罩圖片': 'No mask image selected',
    '編輯遮罩': 'Edit mask',
    '清除遮罩': 'Clear mask',
    '控制圖': 'Control image',
    'ControlNet 會依這張控制圖保留構圖、姿勢、深度或線稿。': 'ControlNet uses this control image to preserve composition, pose, depth, or line art.',
    '控制圖只在啟用 ControlNet 時送出。': 'The control image is sent only when ControlNet is enabled.',
    '尚未選擇控制圖': 'No control image selected',
    '清除控制圖': 'Clear control image',
    '啟用 ControlNet': 'Enable ControlNet',
    '可控制構圖、姿勢、線稿、深度、邊緣或局部細節，不只是重畫原圖。': 'Controls composition, pose, line art, depth, edges, or local details; it is not just repainting the original image.',
    'ControlNet 類型': 'ControlNet type',
    'ControlNet 模型': 'ControlNet model',
    '自動選擇可用模型': 'Auto-select available model',
    '自動選擇': 'Auto select',
    'Canny 適合保留邊緣與輪廓，常用於重畫原圖構圖。': 'Canny is useful for preserving edges and outlines and is often used to redraw the original composition.',
    '向外延展範圍': 'Outpaint range',
    '向外延展會先在原圖外圍補邊，再交給模型接續生成。': 'Outpaint first pads around the original image, then lets the model continue generation.',
    '新增 LoRA': 'Add LoRA',
    'LoRA 每增加 1 個，成功產圖每張額外 +1 點。': 'Each additional LoRA adds +1 point per successfully generated image.',
    '讀取 LoRA 中...': 'Loading LoRA...',
    '尚未選擇 LoRA': 'No LoRA selected',
    '加入': 'Add',
    '加入相簿': 'Add to album',
    '不加入相簿': 'Do not add to album',
    '提示詞': 'Prompt',
    'Embedding 快速插入': 'Embedding quick insert',
    '讀取 Embedding 中...': 'Loading embeddings...',
    '點一下會把': 'Click once to insert',
    '插入正向提示詞；送出前會自動轉成 ComfyUI 可用格式。': 'into the positive prompt; it is automatically converted to ComfyUI format before submission.',
    '負面提示詞': 'Negative prompt',
    '寬度': 'Width',
    '高度': 'Height',
    '步數': 'Steps',
    '張數': 'Images',
    '執行次數': 'Runs',
    'Seed（空白=隨機）': 'Seed (blank=random)',
    '儲存到資料夾': 'Save to folder',
    '分享標題': 'Share title',
    '心得留言': 'Comment',
    '產生圖片': 'Generate image',
    '中斷產圖': 'Interrupt generation',
    '存到雲端硬碟': 'Save to Cloud Drive',
    '分享到 ComfyUI 專區': 'Share to ComfyUI area',
    '丟棄預覽': 'Discard preview',
    '遮罩編輯器': 'Mask editor',
    '畫筆': 'Brush',
    '橡皮擦': 'Eraser',
    '筆刷': 'Brush size',
    '全選': 'Select all',
    '反相': 'Invert',
    '套用遮罩': 'Apply mask',
    '白色區域會交給 inpaint 重繪。': 'White areas will be redrawn by inpaint.',
    '在來源圖上拖曳即可畫出要重繪的位置；可用滑鼠、觸控筆或手機觸控操作。': 'Drag on the source image to mark areas to redraw; mouse, stylus, and mobile touch are supported.',
    '讀取歷史產圖與雲端硬碟圖片。': 'Load historical generations and Cloud Drive images.',
    '尚未讀取圖片清單': 'Image list has not been loaded',
    '產圖結果': 'Generation result',
    '尚未產生圖片': 'No image generated yet',
    '等待 ComfyUI 回應': 'Waiting for ComfyUI response',
    'ComfyUI 歷史重跑': 'ComfyUI history rerun',
    '保存模式、來源圖、ControlNet、放大模型與重繪參數，可一鍵套回或重跑。': 'Save mode, source image, ControlNet, upscale model, and redraw parameters so they can be applied or rerun with one click.',
    '重新整理歷史': 'Refresh history',
    '尚未讀取歷史紀錄': 'History has not been loaded',
    '尚無 ComfyUI 歷史紀錄': 'No ComfyUI history yet',
    '重新整理 Workflow': 'Refresh workflows',
    '尚未讀取 workflow preset': 'Workflow presets have not been loaded',
    '可新增、編輯、儲存、匯入、匯出自訂工作流版面；官方版面與個人版面分開管理。': 'Add, edit, save, import, and export custom workflow layouts; official and personal layouts are managed separately.',
    '版面名稱': 'Layout name',
    '適用用途': 'Use case',
    'ComfyUI 版本': 'ComfyUI version',
    '本專案版本': 'Project version',
    '匯入 JSON 檔': 'Import JSON file',
    '設為我的預設版面': 'Set as my default layout',
    '匯入的 workflow 會檢查節點結構、絕對路徑、外部 URL、惡意 shell/exec 片段；缺模型不會靜默 fallback。': 'Imported workflows check node structure, absolute paths, external URLs, and malicious shell/exec fragments; missing models never silently fall back.',
    '新增空白版面': 'New blank layout',
    '建立 txt2img 起始版': 'Create txt2img starter layout',
    '開啟節點連線編輯器': 'Open node graph editor',
    '載入視覺編輯器結果': 'Load visual editor result',
    '匯出目前 Workflow': 'Export current workflow',
    '新增版面': 'Add layout',
    '更新目前選擇': 'Update current selection',
    '清空編輯器': 'Clear editor',
    '快速追加節點': 'Quick add node',
    '選擇節點': 'Choose node',
    '節點顯示名稱': 'Node display name',
    '追加節點': 'Add node',
    '視覺編輯器結果載入後會在這裡顯示節點摘要。': 'After visual editor results load, node summaries appear here.',
    '進階 JSON 匯入 / 除錯': 'Advanced JSON import / debug',
    '一般使用者請用視覺編輯器；這裡只保留匯入外部 workflow 或除錯。': 'Regular users should use the visual editor; this area is only for importing external workflows or debugging.',
    '可先從目前表單匯出 workflow，再保存成版面；編輯後需按「新增版面」或「更新目前選擇」才會保存。': 'Export a workflow from the current form first, then save it as a layout. After editing, press Add layout or Update current selection to save.',
    '我的工作流版面': 'My workflow layouts',
    '尚無個人版面': 'No personal layouts yet',
    '官方工作流版面': 'Official workflow layouts',
    '尚無官方版面': 'No official layouts yet',
    '其他可讀版面': 'Other readable layouts',
    '尚無其他可讀版面': 'No other readable layouts yet',
    'root 模型匯入（Civitai / 檔案上傳）': 'Root model import (Civitai / file upload)',
    '展開後可從 Civitai 下載，或直接上傳模型檔到本地 ComfyUI models 目錄；和上方生圖表單分開。': 'Expand to download from Civitai or upload model files directly into the local ComfyUI models directory; this is separate from the generation form above.',
    '目前是本地模式，可在這裡管理 Civitai 模型下載。': 'Current mode is local; manage Civitai model downloads here.',
    'ComfyUI 專案資料夾': 'ComfyUI project folder',
    '模型類型': 'Model type',
    '模型來源': 'Model source',
    'Civitai 網址': 'Civitai URL',
    '本地檔案上傳': 'Local file upload',
    '下載 / 匯入到相對路徑': 'Download / import to relative path',
    '可先用關鍵字搜尋與篩選 Civitai，搜尋結果只會幫你帶入模型網址；真正下載前仍會再次確認版本、檔案、大小與 hash。': 'Search and filter Civitai by keyword first. Search results only fill the model URL; version, file, size, and hash are confirmed again before real download.',
    '關鍵字搜尋': 'Keyword search',
    '全部': 'All',
    '類型': 'Type',
    '安全篩選': 'Safety filter',
    '僅 Safe': 'Safe only',
    '僅 NSFW': 'NSFW only',
    '尚未搜尋 Civitai 模型': 'No Civitai model search yet',
    '支援關鍵字、base model、Checkpoint / LoRA / Embedding / ControlNet、Safe/NSFW 篩選。': 'Supports keyword, base model, Checkpoint / LoRA / Embedding / ControlNet, and Safe/NSFW filtering.',
    'Civitai 模型頁網址': 'Civitai model page URL',
    '版本': 'Version',
    '匯入': 'Import',
    '匯出': 'Export',
    '官方': 'Official',
    '個人': 'Personal',
    '版面': 'Layout',
    '工作流': 'Workflow',
    '視覺編輯器': 'Visual editor',
    '關鍵字': 'Keyword',
    '篩選': 'Filter',
    '本地': 'Local',
    '資料夾': 'Folder',
    '檔案上傳': 'File upload',
    '建立可回測、可執行的節點式交易策略；儲存後回交易頁載入。': 'Build node-based trading strategies that can be backtested and executed; save them and return to the trading page to load.',
    '用節點與線建立 ComfyUI workflow；從綠色 output 拉到紫色 input，完成後回主頁保存。': 'Build a ComfyUI workflow with nodes and links; connect green outputs to purple inputs, then return to the main page to save.',
    '載入範例': 'Load example',
    '套用 JSON': 'Apply JSON',
    '複製 JSON': 'Copy JSON',
    '節點工具箱': 'Node toolbox',
    'Graph 節點': 'Graph nodes',
    '條件節點': 'Condition nodes',
    '節點屬性': 'Node properties',
    '回測預覽': 'Backtest preview',
    '開始時間': 'Start time',
    '結束時間': 'End time',
    '執行回測預覽': 'Run backtest preview',
    '最近 180 天': 'Last 180 days',
    '最近 365 天': 'Last 365 days',
    '可讀 JSON': 'Readable JSON',
    '重新排列節點': 'Auto layout nodes',
    '匯入 JSON': 'Import JSON',
    '送回主頁': 'Send back to main page',
    '下載 ComfyUI Workflow': 'Download ComfyUI Workflow',
    '下載 API Prompt': 'Download API Prompt',
    '下載本站 Preset': 'Download site preset',
    '複製 ComfyUI Workflow': 'Copy ComfyUI Workflow',
    '搜尋節點': 'Search nodes',
    '目前 ComfyUI 節點': 'Current ComfyUI nodes',
    '依賴 / 驗證': 'Dependencies / validation',
    '連線': 'Connections',
    '未選取': 'None selected',
    '本站 preset/layout JSON': 'Site preset/layout JSON',
    '例如 checkpoint、inpaint、upscale、control': 'Example: checkpoint, inpaint, upscale, control',
  });

  const CJK_RE = /[\u3400-\u9fff]/;
  const PHRASE_KEYS = Object.freeze(
    Object.keys(EN_TEXT)
      .filter((key) => CJK_RE.test(key))
      .filter((key) => key.length >= 2)
      .sort((a, b) => b.length - a.length)
  );
  const CJK_PUNCTUATION = Object.freeze({
    '：': ': ',
    '，': ', ',
    '。': '.',
    '；': '; ',
    '、': ', ',
    '（': ' (',
    '）': ')',
    '【': '[',
    '】': ']',
    '「': '"',
    '」': '"',
    '『': '"',
    '』': '"',
    '％': '%',
    '／': ' / ',
    '　': ' ',
  });

  let currentLocale = normalizeLocale(readStoredLocale());
  let observer = null;
  let applying = false;
  let pendingRoots = new Set();
  let pendingFrame = null;

  function readStoredLocale() {
    try {
      return localStorage.getItem(STORAGE_KEY) || DEFAULT_LOCALE;
    } catch (err) {
      return DEFAULT_LOCALE;
    }
  }

  function normalizeLocale(value) {
    const raw = String(value || '').trim();
    const lower = raw.toLowerCase();
    if (lower === 'en' || lower.startsWith('en-')) return 'en';
    if (lower === 'zh' || lower === 'zh-tw' || lower === 'zh-hant' || lower === 'zh-hk') return 'zh-TW';
    return DEFAULT_LOCALE;
  }

  function translateKey(key, vars = null) {
    const entry = KEY_TEXT[String(key || '')];
    const text = entry ? (entry[currentLocale] || entry[DEFAULT_LOCALE] || '') : '';
    return interpolate(text || String(key || ''), vars);
  }

  function translateSourceText(source) {
    return translateSourceTextForLocale(source, currentLocale);
  }

  function translateSourceTextForLocale(source, locale) {
    const value = String(source ?? '');
    if (locale === DEFAULT_LOCALE) return value;
    return EN_TEXT[value] || translateByPhraseFallback(value);
  }

  function hasCjkText(value) {
    return CJK_RE.test(String(value || ''));
  }

  function translateByPhraseFallback(value) {
    const source = String(value ?? '');
    if (!hasCjkText(source)) return source;
    let result = source;
    PHRASE_KEYS.forEach((key) => {
      if (!key || !result.includes(key)) return;
      const replacement = String(EN_TEXT[key] || '').trim();
      if (!replacement) return;
      result = result.split(key).join(` ${replacement} `);
    });
    Object.entries(CJK_PUNCTUATION).forEach(([from, to]) => {
      if (result.includes(from)) result = result.split(from).join(to);
    });
    return result
      .replace(/\s+/g, ' ')
      .replace(/\s+([,.;:!?%\]\)])/g, '$1')
      .replace(/([\[\(])\s+/g, '$1')
      .replace(/\s+\/\s+/g, ' / ')
      .trim();
  }

  function interpolate(text, vars = null) {
    if (!vars || typeof vars !== 'object') return text;
    return String(text || '').replace(/\{([a-zA-Z0-9_]+)\}/g, (_match, key) => (
      Object.prototype.hasOwnProperty.call(vars, key) ? String(vars[key]) : ''
    ));
  }

  function translatedPreservingWhitespace(source) {
    return translatedPreservingWhitespaceForLocale(source, currentLocale);
  }

  function translatedPreservingWhitespaceForLocale(source, locale) {
    const raw = String(source ?? '');
    const match = raw.match(/^(\s*)([\s\S]*?)(\s*)$/);
    if (!match) return translateSourceTextForLocale(raw, locale);
    return `${match[1]}${translateSourceTextForLocale(match[2], locale)}${match[3]}`;
  }

  function isKnownRenderedText(current, source) {
    return Object.keys(SUPPORTED_LOCALES).some((locale) => (
      current === translatedPreservingWhitespaceForLocale(source, locale)
    ));
  }

  function isKnownRenderedAttribute(current, source) {
    return Object.keys(SUPPORTED_LOCALES).some((locale) => (
      current === translateSourceTextForLocale(source, locale)
    ));
  }

  function shouldSkipElement(element) {
    return !element || Boolean(element.closest?.(SKIP_SELECTOR));
  }

  function scopedElementRoot(root) {
    if (!root || root === document || root.nodeType === Node.DOCUMENT_NODE) return document;
    if (root.nodeType === Node.ELEMENT_NODE) return root;
    return null;
  }

  function shouldSkipTextNode(node) {
    const parent = node?.parentElement;
    if (!parent || shouldSkipElement(parent)) return true;
    if (parent.closest?.('textarea,input')) return true;
    if (parent.closest?.('[data-i18n]')) return true;
    const trimmed = String(node.nodeValue || '').trim();
    if (!trimmed) return true;
    return (
      !Object.prototype.hasOwnProperty.call(EN_TEXT, trimmed)
      && !(currentLocale !== DEFAULT_LOCALE && hasCjkText(trimmed))
      && !node.__hackmeI18nSourceText
    );
  }

  function translateTextNode(node) {
    if (shouldSkipTextNode(node)) return;
    const current = String(node.nodeValue || '');
    const existingSource = node.__hackmeI18nSourceText;
    let source = existingSource;
    if (!source || !isKnownRenderedText(current, source)) {
      source = current;
      node.__hackmeI18nSourceText = source;
    }
    const next = translatedPreservingWhitespace(source);
    if (current !== next) node.nodeValue = next;
  }

  function attrStoreName(attr) {
    return `hackmeI18nSource${attr.replace(/[^a-z0-9]/gi, '_')}`;
  }

  function translateAttribute(element, attr) {
    if (!element || shouldSkipElement(element) || !element.hasAttribute(attr)) return;
    const current = element.getAttribute(attr) || '';
    const trimmed = current.trim();
    const store = attrStoreName(attr);
    const existingSource = element.dataset[store] || '';
    if (
      !existingSource
      && !Object.prototype.hasOwnProperty.call(EN_TEXT, trimmed)
      && !(currentLocale !== DEFAULT_LOCALE && hasCjkText(trimmed))
    ) return;
    let source = existingSource;
    if (!source || !isKnownRenderedAttribute(current, source)) {
      source = current;
      element.dataset[store] = source;
    }
    const next = translateSourceText(source);
    if (current !== next) element.setAttribute(attr, next);
  }

  function applyExplicitTranslations(root) {
    const scope = scopedElementRoot(root);
    if (!scope) return;
    const elements = [];
    if (root?.nodeType === Node.ELEMENT_NODE && root.matches?.('[data-i18n]')) elements.push(root);
    elements.push(...scope.querySelectorAll('[data-i18n]'));
    elements.forEach((element) => {
      const key = element.getAttribute('data-i18n');
      if (!key) return;
      element.textContent = translateKey(key);
    });
  }

  function translateAttributes(root) {
    const scope = scopedElementRoot(root);
    if (!scope) return;
    const elements = [];
    if (root?.nodeType === Node.ELEMENT_NODE) elements.push(root);
    elements.push(...scope.querySelectorAll(ATTRIBUTE_NAMES.map((attr) => `[${attr}]`).join(',')));
    elements.forEach((element) => {
      ATTRIBUTE_NAMES.forEach((attr) => translateAttribute(element, attr));
    });
  }

  function translateTextNodes(root) {
    const start = root || document.body;
    if (!start) return;
    if (start.nodeType === Node.TEXT_NODE) {
      translateTextNode(start);
      return;
    }
    if (start.nodeType !== Node.ELEMENT_NODE && start.nodeType !== Node.DOCUMENT_NODE) return;
    const walker = document.createTreeWalker(start, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        return shouldSkipTextNode(node) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
      },
    });
    while (walker.nextNode()) translateTextNode(walker.currentNode);
  }

  function syncLanguageControls(root = document) {
    const scope = scopedElementRoot(root);
    if (!scope) return;
    const controls = [];
    if (root?.nodeType === Node.ELEMENT_NODE && root.matches?.('[data-language-select]')) controls.push(root);
    controls.push(...scope.querySelectorAll('[data-language-select]'));
    controls.forEach((select) => {
      if (!select.dataset.i18nBound) {
        select.dataset.i18nBound = '1';
        select.addEventListener('change', () => setLocale(select.value, { announce: true }));
      }
      select.value = currentLocale;
      const zh = select.querySelector('option[value="zh-TW"]');
      const en = select.querySelector('option[value="en"]');
      if (zh) zh.textContent = currentLocale === 'en' ? 'Traditional Chinese' : '繁體中文';
      if (en) en.textContent = 'English';
    });
  }

  function applyI18n(root = document) {
    if (!document.body) return;
    const isWholeDocument = root === document || root?.nodeType === Node.DOCUMENT_NODE;
    applying = true;
    try {
      if (isWholeDocument) {
        document.documentElement.lang = currentLocale;
        const titleKey = document.documentElement.dataset.i18nTitle || document.body?.dataset.i18nTitle || 'app.title';
        document.title = translateKey(titleKey);
      }
      if (root?.nodeType === Node.TEXT_NODE) {
        translateTextNode(root);
        return;
      }
      syncLanguageControls(root);
      applyExplicitTranslations(root);
      translateAttributes(root);
      translateTextNodes(root);
      if (isWholeDocument) document.body.dataset.locale = currentLocale;
    } finally {
      applying = false;
    }
  }

  function queueApply(root) {
    if (applying || !root) return;
    pendingRoots.add(root);
    if (pendingFrame) return;
    pendingFrame = requestAnimationFrame(() => {
      const roots = Array.from(pendingRoots);
      pendingRoots.clear();
      pendingFrame = null;
      roots.forEach((item) => applyI18n(item));
    });
  }

  function startObserver() {
    if (observer || !document.body) return;
    observer = new MutationObserver((mutations) => {
      if (applying) return;
      mutations.forEach((mutation) => {
        if (mutation.type === 'childList') {
          mutation.addedNodes.forEach((node) => queueApply(node));
          return;
        }
        if (mutation.type === 'characterData') {
          queueApply(mutation.target);
          return;
        }
        if (mutation.type === 'attributes') {
          queueApply(mutation.target);
        }
      });
    });
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: ATTRIBUTE_NAMES,
    });
  }

  function setLocale(locale, options = {}) {
    const next = normalizeLocale(locale);
    currentLocale = next;
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch (err) {}
    applyI18n(document);
    document.dispatchEvent(new CustomEvent('hackme:locale-changed', { detail: { locale: next } }));
    if (options.announce && typeof window.showAppToast === 'function') {
      window.showAppToast(translateKey('language.changed'), true, { duration: 1200 });
    }
  }

  function init() {
    syncLanguageControls(document);
    applyI18n(document);
    startObserver();
  }

  window.HackmeI18n = {
    supportedLocales: SUPPORTED_LOCALES,
    getLocale: () => currentLocale,
    setLocale,
    t: translateKey,
    text: translateSourceText,
    apply: applyI18n,
  };
  window.t = translateKey;
  window.translateUiText = translateSourceText;
  window.setAppLocale = setLocale;
  window.getAppLocale = () => currentLocale;
  window.applyI18n = applyI18n;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
