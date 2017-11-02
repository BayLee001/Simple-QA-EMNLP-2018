from lib.text_encoders.word_encoder import WordEncoder


class MosesEncoder(WordEncoder):

    def __init__(self, *args, **kwargs):
        if 'tokenize' in kwargs:
            raise TypeError('MosesEncoder defines a tokenize callable Moses')

        import nltk

        # Required for moses
        nltk.download('perluniprops')
        nltk.download('nonbreaking_prefixes')

        from nltk.tokenize.moses import MosesTokenizer

        super().__init__(*args, **kwargs, tokenize=MosesTokenizer().tokenize)
