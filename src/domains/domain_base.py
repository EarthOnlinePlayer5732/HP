from pathlib import Path

from utils_context import stringify_context, format_file_names_for_prompt


class DomainBase:
    supports_visual = False  # override in subclasses that implement render_context_visual

    def __init__(self, prompt_file):
        self.sample_type = None
        self.description = ""          # short (<5 word) domain description
        self.file_format = []           # list of file extensions, e.g. [".ledger"]
        self.domain_parser = "custom"   # parsing library name, or "custom"
        self.category = ""             # one of: science, code, creative, records, everyday
        self.samples_folder = None         # supplied by the campaign runtime
        prompt_path = Path(prompt_file)
        if not prompt_path.is_absolute():
            prompt_path = Path(__file__).resolve().parents[2] / prompt_path
        self.prompt_template = prompt_path.read_text(encoding="utf-8")

    def preprocess_context(self, context):
        """Normalize raw context string before parsing. Override in subclasses to fix common LLM syntax issues."""
        return context

    def parse_context(self, context):
        # Override this method in subclasses
        raise NotImplementedError("Subclasses must implement parse_context()")

    def compute_domain_statistics(self, context):
        # Override this method in subclasses
        raise NotImplementedError("Subclasses must implement compute_domain_statistics()")

    def evaluate_context(self, sample_id, generated_context, target_state):
        # Override this method in subclasses
        raise NotImplementedError("Subclasses must implement evaluate_context()")

    def render_context_visual(self, context, outfile):
        """Render a context dict to a visual image.

        Args:
            context: dict mapping filename -> file content string
            outfile: output path *without* extension; the method appends the
                     appropriate suffix (e.g. '.png').

        Returns:
            The actual output file path (with extension), or None if this
            domain does not support visual rendering.

        Override in subclasses and set ``supports_visual = True``.
        """
        return None


    def prepare_prompt(self, current_context, target_state, edit_operation, **kwargs):
        context_str = stringify_context(current_context)
        target_context = target_state["context"]
        file_names = format_file_names_for_prompt(target_context)

        prompt_populated = self.prompt_template.replace("[[INPUT_CONTEXT]]", context_str).replace("[[FILE_NAMES]]", file_names).replace("[[EDITING_OPERATION]]", edit_operation)
        return prompt_populated
