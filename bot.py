import concurrent
import os
import queue

import discord
from discord.ext import commands, voice_recv, tasks
import asyncio
import logging
import uuid
import time
import openai
from pydub import AudioSegment
from io import BytesIO
from dotenv import load_dotenv

from position_tracking_pcm_audio import PositionTrackingPCMAudio

load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Initialize OpenAI client (ensure you've set up your API key)
client = openai.OpenAI(
    api_key=os.getenv('OPENAI_API_KEY')
)


class Response:
    def __init__(self, filename, text, id=None):
        self.id = id or str(uuid.uuid4())
        self.filename = filename
        self.text = text
        self.position = 0


class VoiceActivityDetector:
    def __init__(self, sample_rate=48000, frame_duration=20, silence_threshold=500, speech_threshold=1000,
                 min_speech_duration=0.3, max_silence_duration=0.5):
        self.sample_rate = sample_rate
        self.frame_duration = frame_duration
        self.silence_threshold = silence_threshold
        self.speech_threshold = speech_threshold
        self.min_speech_duration = min_speech_duration
        self.max_silence_duration = max_silence_duration
        self.frame_size = int(sample_rate * (frame_duration / 1000))
        self.is_speech = False
        self.speech_start_time = None
        self.last_speech_time = None
        self.audio_buffer = bytearray()
        self.speech_buffer = bytearray()

    def process_audio(self, voice_data):
        if hasattr(voice_data, 'pcm'):
            audio_data = voice_data.pcm
        elif hasattr(voice_data, 'data'):
            audio_data = voice_data.data
        else:
            logging.error(
                f"Unable to extract audio data from VoiceData object. Available attributes: {dir(voice_data)}")
            return None, 0

        self.audio_buffer.extend(audio_data)

        processed_data = None
        speech_duration = 0

        while len(self.audio_buffer) >= self.frame_size:
            frame = self.audio_buffer[:self.frame_size]
            self.audio_buffer = self.audio_buffer[self.frame_size:]

            rms = (sum(int.from_bytes(frame[i:i + 2], 'little', signed=True) ** 2 for i in range(0, len(frame), 2)) / (
                    len(frame) // 2)) ** 0.5

            current_time = time.time()

            if rms > self.speech_threshold:
                if not self.is_speech:
                    self.is_speech = True
                    self.speech_start_time = current_time
                    logging.info(f"Speech started, RMS: {rms}")
                self.last_speech_time = current_time
                self.speech_buffer.extend(frame)
            elif rms < self.silence_threshold:
                if self.is_speech:
                    self.speech_buffer.extend(frame)
                if self.is_speech and (current_time - self.last_speech_time) > self.max_silence_duration:
                    self.is_speech = False
                    speech_duration = self.last_speech_time - self.speech_start_time
                    logging.info(f"Speech ended, duration: {speech_duration:.2f}s, RMS: {rms}")
                    if speech_duration >= self.min_speech_duration:
                        processed_data = bytes(self.speech_buffer)
                        logging.info(f"Returning speech data of length: {len(processed_data)} bytes")
                        self.speech_buffer = bytearray()
                        return processed_data, speech_duration

        return processed_data, speech_duration


class InterruptibleVoiceClient(voice_recv.VoiceRecvClient):
    def __init__(self, *args, voice_commands=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_response = None
        self.last_interrupted_response = None
        self.response_queue = asyncio.Queue()
        self.speaking_task = None
        self.voice_commands = voice_commands
        logging.debug("InterruptibleVoiceClient initialized")

    async def add_response(self, response):
        await self.response_queue.put(response)
        if not self.speaking_task or self.speaking_task.done():
            self.speaking_task = asyncio.create_task(self.process_responses())

    async def process_responses(self):
        while True:
            try:
                response = await self.response_queue.get()
                await self.speak(response)
                if self.response_queue.empty():
                    break
            except Exception as e:
                logging.error(f"Error in process_responses: {str(e)}")
                logging.exception("Exception details:")

    async def speak(self, response, resume_position=0):
        logging.info(f"Starting to speak response {response.id}")
        self.current_response = response
        try:
            if self.voice_commands:
                self.voice_commands.set_speaking(self.guild.id, True)

            audio_source = discord.FFmpegPCMAudio(response.filename)
            tracking_source = PositionTrackingPCMAudio(audio_source)

            if resume_position > 0:
                logging.debug(f"Resuming from position {resume_position}")
                while tracking_source.position < resume_position:
                    tracking_source.read()

            self.play(tracking_source)

            while self.is_playing():
                await asyncio.sleep(0.1)
                if self.current_response:
                    self.current_response.position = tracking_source.position

        except Exception as e:
            logging.error(f"Error during speak: {e}")
        finally:
            logging.info(f"Finished speaking response {response.id}")
            self.current_response = None
            if self.voice_commands:
                self.voice_commands.set_speaking(self.guild.id, False)

    def interrupt(self):
        logging.info("Interrupting current speech")
        if self.is_playing():
            self.last_interrupted_response = self.current_response
            self.stop()
            if self.voice_commands:
                self.voice_commands.set_speaking(self.guild.id, False)

    async def resume_last_response(self):
        if self.last_interrupted_response:
            logging.info(f"Resuming last interrupted response {self.last_interrupted_response.id}")
            await self.speak(self.last_interrupted_response, self.last_interrupted_response.position)
            self.last_interrupted_response = None
        else:
            logging.warning("No interrupted response to resume")

    def is_speaking(self):
        return self.is_playing()



import functools

import concurrent.futures

import asyncio
import time
from discord.ext import commands




class VoiceCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.voice_clients = {}
        self.voice_data_queues = {}
        self.vad_detectors = {}
        self.short_interruption_threshold = 2.0  # 2 seconds threshold
        self.thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=5)
        self.last_speech_time = {}
        self.last_bot_speech_time = {}
        self.is_speaking = {}
        self.audio_processor_tasks = {}
        self.should_process_audio = {}
        logging.debug("VoiceCommands initialized")

    @commands.command()
    async def join(self, ctx):
        if ctx.author.voice:
            channel = ctx.author.voice.channel
            voice_client = await channel.connect(cls=lambda *args, **kwargs: InterruptibleVoiceClient(*args, **kwargs, voice_commands=self))
            logging.info(f"Joined voice channel: {channel}")

            guild_id = ctx.guild.id
            self.voice_clients[guild_id] = voice_client
            self.voice_data_queues[guild_id] = asyncio.Queue()
            self.vad_detectors[guild_id] = VoiceActivityDetector()
            self.last_speech_time[guild_id] = 0
            self.last_bot_speech_time[guild_id] = 0
            self.is_speaking[guild_id] = False
            self.should_process_audio[guild_id] = True

            def audio_callback(user, voice_data):
                asyncio.run_coroutine_threadsafe(self.voice_data_queues[guild_id].put((user, voice_data)), self.bot.loop)
                logging.debug(f"Audio data queued for guild {guild_id}")

            voice_client.listen(voice_recv.BasicSink(audio_callback))

            self.audio_processor_tasks[guild_id] = asyncio.create_task(self.audio_processor(guild_id))
            logging.info(f"Started audio processor task for guild {guild_id}")

            await ctx.send(f"Joined {channel}. I'm now listening and will respond when you finish speaking.")
        else:
            await ctx.send("You need to be in a voice channel to use this command.")

    async def audio_processor(self, guild_id):
        logging.info(f"Audio processor started for guild {guild_id}")
        while self.should_process_audio[guild_id]:
            try:
                logging.debug(f"Waiting for voice data in guild {guild_id}")
                user, voice_data = await asyncio.wait_for(self.voice_data_queues[guild_id].get(), timeout=1.0)
                logging.debug(f"Received voice data for guild {guild_id}")
                await self.process_voice_data(user, voice_data, guild_id, self.voice_clients[guild_id])
            except asyncio.TimeoutError:
                logging.debug(f"No voice data received for guild {guild_id} in the last second")
            except asyncio.CancelledError:
                logging.info(f"Audio processor for guild {guild_id} was cancelled")
                break
            except Exception as e:
                logging.error(f"Error in audio_processor for guild {guild_id}: {str(e)}")
                logging.exception("Exception details:")
            finally:
                # Ensure we're always ready to process the next piece of data
                self.is_speaking[guild_id] = False
            await asyncio.sleep(0.1)
        logging.info(f"Audio processor stopped for guild {guild_id}")

    async def process_voice_data(self, user, voice_data, guild_id, voice_client):
        try:
            processed_data, speech_duration = self.vad_detectors[guild_id].process_audio(voice_data)

            if processed_data:
                current_time = time.time()
                time_since_last_speech = current_time - self.last_speech_time[guild_id]
                time_since_bot_speech = current_time - self.last_bot_speech_time[guild_id]
                self.last_speech_time[guild_id] = current_time

                logging.info(f"Received complete voice data from {user}, duration: {speech_duration:.2f}s, "
                             f"time since last speech: {time_since_last_speech:.2f}s, "
                             f"time since bot speech: {time_since_bot_speech:.2f}s")

                if self.is_speaking[guild_id]:
                    voice_client.interrupt()
                    logging.info("Bot was speaking, interrupted.")

                    if time_since_bot_speech <= self.short_interruption_threshold:
                        logging.info("Short interruption detected, resuming previous response")
                        await voice_client.resume_last_response()
                    else:
                        logging.info("Long interruption detected, processing as new input")
                        await self.process_new_input(processed_data, voice_client, user)
                else:
                    logging.info("Bot was not speaking, processing as new input")
                    await self.process_new_input(processed_data, voice_client, user)

        except Exception as e:
            logging.error(f"Error in process_voice_data: {str(e)}")
            logging.exception("Exception details:")
        finally:
            # Ensure we're always ready to process the next piece of data
            self.is_speaking[guild_id] = False

    async def process_new_input(self, processed_data, voice_client, user):
        logging.info("Processing new input")
        user_info = {
            'name': user.name,
            'id': user.id,
            'roles': [role.name for role in user.roles if role.name != "@everyone"]
        }
        await self.process_audio(processed_data, voice_client, user_info)

    async def process_audio(self, audio_data, voice_client, user_info):
        try:
            logging.info("Starting to process audio")

            # Run OpenAI API calls in a separate thread
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                self.thread_pool,
                functools.partial(self.openai_process, audio_data, user_info)
            )

            if response is None:
                logging.warning("Failed to process audio")
                return

            speech_content, response_text = response

            # Save the audio file
            response_id = str(uuid.uuid4())
            filename = f"response_{response_id}.mp3"
            with open(filename, "wb") as f:
                f.write(speech_content)

            response_obj = Response(filename, response_text, id=response_id)
            logging.info(f"Generated response with ID: {response_obj.id}")

            # Add the response to the voice client's queue
            if isinstance(voice_client, InterruptibleVoiceClient) and voice_client.is_connected():
                await voice_client.add_response(response_obj)
                guild_id = voice_client.guild.id
                self.last_bot_speech_time[guild_id] = time.time()
                self.is_speaking[guild_id] = True
            else:
                logging.warning("Voice client is not connected or not interruptible. Unable to play audio response.")

        except Exception as e:
            logging.error(f"Error in process_audio: {str(e)}")
            logging.exception("Exception details:")

    @commands.command()
    async def leave(self, ctx):
        guild_id = ctx.guild.id
        if guild_id in self.voice_clients:
            self.should_process_audio[guild_id] = False
            await self.voice_clients[guild_id].disconnect()
            del self.voice_clients[guild_id]
            del self.voice_data_queues[guild_id]
            del self.vad_detectors[guild_id]
            del self.is_speaking[guild_id]
            del self.last_speech_time[guild_id]
            del self.last_bot_speech_time[guild_id]
            if guild_id in self.audio_processor_tasks:
                self.audio_processor_tasks[guild_id].cancel()
                del self.audio_processor_tasks[guild_id]
            logging.info(f"Left voice channel for guild {guild_id}")
            await ctx.send("Left the voice channel.")
        else:
            await ctx.send("I'm not in a voice channel.")

    def set_speaking(self, guild_id, is_speaking):
        self.is_speaking[guild_id] = is_speaking
        logging.info(f"Set speaking state for guild {guild_id} to {is_speaking}")

    def openai_process(self, audio_data, user_info):
        try:
            # Convert audio data to WAV
            audio = AudioSegment(
                audio_data,
                frame_rate=48000,
                sample_width=2,
                channels=2
            )

            buffer = BytesIO()
            audio.export(buffer, format="wav")
            buffer.seek(0)

            # Transcribe audio
            transcript = client.audio.transcriptions.create(model="whisper-1", file=("audio.wav", buffer))

            logging.info(f"Transcription: {transcript.text}")

            # Generate GPT response
            chat_completion = client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant in a voice chat."},
                    {"role": "user", "content": f"User {user_info['name']} said: {transcript.text}"}
                ]
            )
            gpt_response = chat_completion.choices[0].message.content

            logging.info(f"GPT Response: {gpt_response}")

            # Convert text to speech
            speech_response = client.audio.speech.create(
                model="tts-1",
                voice="alloy",
                input=gpt_response
            )

            return speech_response.content, gpt_response

        except Exception as e:
            logging.error(f"Error in openai_process: {str(e)}")
            return None


# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents)


@bot.event
async def on_ready():
    logging.info(f'Logged in as {bot.user.name}')
    voice_commands = VoiceCommands(bot)
    await bot.add_cog(voice_commands)


# Run the bot
if __name__ == "__main__":
    bot.run(os.getenv('DISCORD_TOKEN'))
