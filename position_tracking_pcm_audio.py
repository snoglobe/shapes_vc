import discord


class PositionTrackingPCMAudio(discord.AudioSource):
    def __init__(self, source):
        self.source = source
        self.position = 0

    def read(self):
        data = self.source.read()
        if data:
            self.position += len(data)
        return data

    def is_opus(self):
        return self.source.is_opus()

    def cleanup(self):
        self.source.cleanup()
