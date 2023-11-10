#!/usr/bin/env python3
"""Implementation of the Chat class."""
import json
import shutil
import uuid
from collections import defaultdict
from pathlib import Path

from loguru import logger

from . import GeneralConstants
from .chat_configs import ChatOptions
from .chat_context import EmbeddingBasedChatContext, FullHistoryChatContext
from .openai_utils import make_api_chat_completion_call
from .tokens import TokenUsageDatabase, get_n_tokens_from_msgs


class Chat:
    """Manages conversations with an AI chat model.

    This class encapsulates the chat behavior, including handling the chat context,
    managing cache directories, and interfacing with the OpenAI API for generating chat
    responses.
    """

    def __init__(self, configs: ChatOptions = None):
        """Initializes a chat instance.

        Args:
            configs (ChatOptions, optional): The configurations for this chat session.

        Raises:
            NotImplementedError: If the context model specified in configs is unknown.
        """
        self.id = uuid.uuid4()

        if configs is None:
            configs = ChatOptions()

        self._passed_configs = configs
        for field in self._passed_configs.model_fields:
            setattr(self, field, self._passed_configs[field])

        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.token_usage = defaultdict(lambda: {"input": 0, "output": 0})
        self.token_usage_db = TokenUsageDatabase(fpath=self.token_usage_db_path)

        if self.context_model == "full-history":
            self.context_handler = FullHistoryChatContext(parent_chat=self)
        elif self.context_model == "text-embedding-ada-002":
            self.context_handler = EmbeddingBasedChatContext(parent_chat=self)
        else:
            raise NotImplementedError(f"Unknown context model: {self.context_model}")

    @property
    def cache_dir(self):
        """Return the cache directory for this chat."""
        return self._cache_dir

    @cache_dir.setter
    def cache_dir(self, value):
        if value is None:
            value = GeneralConstants.CHAT_CACHE_DIR / f"chat_{self.id}"
        self._cache_dir = Path(value)

    def save_cache(self):
        """Store the chat's configs and metadata to the cache directory."""
        self.configs.export(self.configs_file)

        metadata = self.metadata  # Trigger loading metadata if not yet done
        with open(self.metadata_file, "w") as metadata_f:
            json.dump(metadata, metadata_f, indent=2)

    def clear_cache(self):
        """Remove the cache directory."""
        shutil.rmtree(self.cache_dir, ignore_errors=True)

    @property
    def configs_file(self):
        """File to store the chat's configs."""
        return self.cache_dir / "configs.json"

    @property
    def context_file_path(self):
        """Return the path to the file that stores the chat context and history."""
        return self.cache_dir / "embeddings.db"

    @property
    def metadata_file(self):
        """File to store the chat metadata."""
        return self.cache_dir / "metadata.json"

    @property
    def metadata(self):
        """Keep metadata associated with the chat."""
        try:
            _ = self._metadata
        except AttributeError:
            try:
                with open(self.metadata_file, "r") as f:
                    self._metadata = json.load(f)
            except (FileNotFoundError, json.decoder.JSONDecodeError):
                self._metadata = {}
        return self._metadata

    @property
    def configs(self):
        """Return the chat's configs after initialisation."""
        configs_dict = {}
        for field_name in ChatOptions.model_fields:
            configs_dict[field_name] = getattr(self, field_name)
        return ChatOptions.model_validate(configs_dict)

    @property
    def base_directive(self):
        """Return the base directive for the LLM."""
        msg_content = " ".join(
            [
                instruction.strip()
                for instruction in [
                    f"You are {self.assistant_name} (model {self.model}).",
                    f"You are a helpful assistant to {self.username}.",
                    " ".join(
                        [f"{instruct.strip(' .')}." for instruct in self.ai_instructions]
                    ),
                    f"You must remember and follow all directives by {self.system_name}.",
                ]
                if instruction.strip()
            ]
        )
        return {"role": "system", "name": self.system_name, "content": msg_content}

    def __del__(self):
        # Store token usage to database
        for model in [self.model, self.context_model]:
            self.token_usage_db.insert_data(
                model=model,
                n_input_tokens=self.token_usage[model]["input"],
                n_output_tokens=self.token_usage[model]["output"],
            )

        cache_empty = self.cache_dir.exists() and not next(
            self.cache_dir.iterdir(), False
        )
        if self.private_mode or cache_empty:
            self.clear_cache()
        else:
            self.save_cache()

    @classmethod
    def from_dict(cls, configs: dict):
        """Creates a Chat instance from a configuration dictionary.

        Converts the configuration dictionary into a ChatOptions instance
        and uses it to instantiate the Chat class.

        Args:
            configs (dict): The chat configuration options as a dictionary.

        Returns:
            Chat: An instance of Chat initialized with the given configurations.
        """
        return cls(configs=ChatOptions.model_validate(configs))

    @classmethod
    def from_cli_args(cls, cli_args):
        """Creates a Chat instance from CLI arguments.

        Extracts relevant options from the CLI arguments and initializes a Chat instance
        with them.

        Args:
            cli_args: The command line arguments.

        Returns:
            Chat: An instance of Chat initialized with CLI-specified configurations.
        """
        chat_opts = {
            k: v
            for k, v in vars(cli_args).items()
            if k in ChatOptions.model_fields and v is not None
        }
        return cls.from_dict(chat_opts)

    @classmethod
    def from_cache(cls, cache_dir: Path):
        """Loads a chat instance from a cache directory.

        Args:
            cache_dir (Path): The path to the cache directory.

        Returns:
            Chat: An instance of Chat loaded with cached configurations and metadata.
        """
        try:
            with open(cache_dir / "configs.json", "r") as configs_f:
                new = cls.from_dict(json.load(configs_f))
        except FileNotFoundError:
            new = cls()
        return new

    def load_history(self):
        """Load chat history from cache."""
        return self.context_handler.load_history()

    @property
    def initial_greeting(self):
        """Return the initial greeting for the chat."""
        return f"Hello! I'm {self.assistant_name}. How can I assist you today?"

    def respond_user_prompt(self, prompt: str, **kwargs):
        """Respond to a user prompt."""
        yield from self._respond_prompt(prompt=prompt, role="user", **kwargs)

    def respond_system_prompt(self, prompt: str, **kwargs):
        """Respond to a system prompt."""
        yield from self._respond_prompt(prompt=prompt, role="system", **kwargs)

    def yield_response_from_msg(self, prompt_msg: dict, add_to_history: bool = True):
        """Yield response from a prompt message."""
        # Get appropriate context for prompt from the context handler
        prompt_context_request = self.context_handler.get_context(msg=prompt_msg)
        context = prompt_context_request["context_messages"]

        # Update token_usage with tokens used in context handler for prompt
        self.token_usage[self.context_model]["input"] += sum(
            prompt_context_request["tokens_usage"].values()
        )

        contextualised_prompt = [self.base_directive, *context, prompt_msg]
        # Update token_usage with tokens used in chat input
        self.token_usage[self.model]["input"] += get_n_tokens_from_msgs(
            messages=contextualised_prompt, model=self.model
        )

        # Make API request and yield response chunks
        full_reply_content = ""
        for chunk in make_api_chat_completion_call(
            conversation=contextualised_prompt, chat_obj=self
        ):
            full_reply_content += chunk
            yield chunk

        # Update token_usage ith tokens used in chat output
        reply_as_msg = {"role": "assistant", "content": full_reply_content}
        self.token_usage[self.model]["output"] += get_n_tokens_from_msgs(
            messages=[reply_as_msg], model=self.model
        )

        if add_to_history:
            # Put current chat exchange in context handler's history
            history_entry_reg_tokens_usage = self.context_handler.add_to_history(
                msg_list=[
                    prompt_msg,
                    {"role": "assistant", "content": full_reply_content},
                ]
            )

            # Update token_usage with tokens used in context handler for reply
            self.token_usage[self.context_model]["output"] += sum(
                history_entry_reg_tokens_usage.values()
            )

    def start(self):
        """Start the chat."""
        # ruff: noqa: T201
        print(f"{self.assistant_name}> {self.initial_greeting}\n")
        try:
            while True:
                question = input(f"{self.username}> ").strip()
                if not question:
                    continue
                print(f"{self.assistant_name}> ", end="", flush=True)
                for chunk in self.respond_user_prompt(prompt=question):
                    print(chunk, end="", flush=True)
                print()
                print()
        except (KeyboardInterrupt, EOFError):
            print("", end="\r")
            logger.info("Exiting chat.")

    def report_token_usage(self, current_chat: bool = True):
        """Report token usage and associated costs."""
        self.token_usage_db.print_usage_costs(self.token_usage, current_chat=current_chat)

    def _respond_prompt(self, prompt: str, role: str, **kwargs):
        prompt_as_msg = {"role": role.lower().strip(), "content": prompt.strip()}
        yield from self.yield_response_from_msg(prompt_as_msg, **kwargs)

    @property
    def api_connection_error_msg(self):
        """Return the error message for API connection errors."""
        return (
            "Sorry, I'm having trouble communicating with OpenAI. "
            + "Please check the validity of your API key and try again."
            + "If the problem persists, please also take a look at the "
            + "OpenAI status page: https://status.openai.com."
        )