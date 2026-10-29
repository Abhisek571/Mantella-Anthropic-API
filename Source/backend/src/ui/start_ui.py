from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse
import webbrowser
import gradio as gr
from src.config.config_loader import ConfigLoader
from src.http.routes.routeable import routeable
from src.ui.settings_ui_constructor import SettingsUIConstructor
import src.utils as utils
import src.extensions as src_extensions

logger = utils.get_logger()

LOCAL_HOSTS = {'localhost', '127.0.0.1', '[::1]', '::1'}


def _host_name(value: str) -> str:
    """'localhost:4999' -> 'localhost', 'http://127.0.0.1:4999' -> '127.0.0.1', '[::1]:4999' -> '[::1]'."""
    value = value.strip().lower().split('://', 1)[-1].split('/', 1)[0]
    if value.startswith('['):
        return value.split(']', 1)[0] + ']'
    return value.rsplit(':', 1)[0] if value.count(':') == 1 else value


def is_local_request(host: str | None, origin: str | None) -> bool:
    """Only this PC may use the server. The game sends no Origin; a web page always does, so a page from any other
    site can't change settings or read keys. Checking Host as well stops DNS rebinding (a site name that resolves
    to 127.0.0.1)."""
    if host and _host_name(host) not in LOCAL_HOSTS:
        return False
    if origin is not None and _host_name(origin) not in LOCAL_HOSTS:
        return False
    return True


def add_local_only_guard(app: FastAPI) -> None:
    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if not is_local_request(request.headers.get('host'), request.headers.get('origin')):
            logger.warning(f"Refused a request to {request.url.path} from Host '{request.headers.get('host')}', "
                           f"Origin '{request.headers.get('origin')}': only this PC may use Mantella's server")
            return PlainTextResponse('Forbidden: Mantella only accepts requests from this PC.', status_code=403)
        return await call_next(request)


class StartUI(routeable):
    BANNER = "docs/_static/img/mantella_banner.png"
    def __init__(self, config: ConfigLoader) -> None:
        super().__init__(config)
        self.__constructor = SettingsUIConstructor()

    def create_main_block(self) -> gr.Blocks:
        with gr.Blocks(title="Mantella", fill_height=True, analytics_enabled=False, theme= self.__get_theme(), css=self.__load_css()) as main_block:
            # with gr.Tab("Settings") as tabs:
            settings_page = self.__generate_settings_page()
            self.__generate_extension_tab()
            # with gr.Tab("Chat with NPCs", interactive=False):
            #     self.__generate_chat_page()
            # with gr.Tab("NPC editor", interactive=False):
            #     self.__generate_character_editor_page()

            with gr.Row(elem_classes="custom-footer"):
                gr.HTML("""
                    <div class="custom-footer">
                        <a href="https://art-from-the-machine.github.io/Mantella/" target="_blank">Mantella Installation Guide</a>
                    </div>
                """)
        return main_block

    def __generate_settings_page(self) -> gr.Column:
        # with gr.Column() as settings:
        for cf in self._config.definitions.base_groups:
            if not cf.is_hidden:
                with gr.Tab(cf.name):
                    cf.accept_visitor(self.__constructor)
        return None #settings
    
    def __generate_extension_tab(self):
        """The "Living World" tab, only when an installed extension adds a section to it, then any tabs of their own."""
        extensions = src_extensions.registry()
        if extensions.has('ui_tab'):
            with gr.Tab("Living World"):
                for name, build in extensions.hook_fns('ui_tab'):
                    try:
                        build(gr)
                    except Exception as e:
                        logger.warning(f"Extension '{name}' failed to build its settings section: {e}")
                        gr.Markdown(f"**{name}** could not show its section: {e}")
        for name, build in extensions.hook_fns('ui_page'):
            try:
                build(gr, self._config)
            except Exception as e:
                logger.warning(f"Extension '{name}' failed to build its settings tab: {e}")

    def __generate_chat_page(self):
        return gr.Column()
    
    def __generate_character_editor_page(self):
        return gr.Column() 

    def __get_theme(self):
        return gr.themes.Soft(primary_hue="green",
                            secondary_hue="green",
                            neutral_hue="zinc",
                            font=['Montserrat', 'ui-sans-serif', 'system-ui', 'sans-serif'],
                            font_mono=['IBM Plex Mono', 'ui-monospace', 'Consolas', 'monospace']).set(
                                input_text_size='*text_md',
                                input_padding='*spacing_md',
                            )

    
    def add_route_to_server(self, app: FastAPI):
        add_local_only_guard(app)

        @app.get("/favicon.ico")
        async def favicon():
            return FileResponse("Mantella.ico")

        gr.mount_gradio_app(app,
                            self.create_main_block(),
                            path="/ui")
        
        link = f'http://localhost:{str(self._config.port)}/ui?__theme=dark'
        logger.log(24, f'\nMantella settings can be changed via this link:')
        logger.log(25, link)
        if self._config.auto_launch_ui == True:
            if not webbrowser.open(link, new=2):
                logger.warning('\nFailed to open Mantella settings UI automatically. To edit settings, see here:')
                logger.log(25, link)
    
    def __load_css(self):
        with open('src/ui/style.css', 'r') as file:
            css_content = file.read()
        return css_content
    
    def _setup_route(self):
        pass

    