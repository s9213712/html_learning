from routes.appeals import register_appeal_routes
from routes.ai_agent import register_ai_agent_routes
from routes.bug_reports import register_bug_report_routes
from routes.community import register_community_routes
from routes.comfyui import register_comfyui_routes
from routes.economy import register_economy_routes
from routes.files import register_file_routes
from routes.games import register_games_routes
from routes.jobs import register_job_routes
from routes.moderation import register_moderation_routes
from routes.reports_notifications import register_reports_notification_routes
from routes.share_management import register_share_management_routes
from routes.system_admin import register_system_admin_routes
from routes.trading import register_trading_routes
from routes.videos import register_video_routes


def register_operation_routes(app, deps):
    register_community_routes(app, deps)
    register_bug_report_routes(app, deps)
    register_economy_routes(app, deps)
    register_trading_routes(app, deps)
    register_video_routes(app, deps)
    register_file_routes(app, deps)
    register_job_routes(app, deps)
    register_share_management_routes(app, deps)
    register_games_routes(app, deps)
    register_ai_agent_routes(app, deps)
    register_comfyui_routes(app, deps)
    register_appeal_routes(app, deps)
    register_reports_notification_routes(app, deps)
    register_moderation_routes(app, deps)
    register_system_admin_routes(app, deps)
