tests: refin refout
	./nosetests test_aroma.py

refin:
	curl -L https://github.com/rtrhd/test-data/raw/master/icaaroma/0.4.0/refin.tar.bz2 | bunzip2 | tar x

refout:
	curl -L https://github.com/rtrhd/test-data/raw/master/icaaroma/0.4.0/refout.tar.bz2 | bunzip2 |tar x

plots: refout
	mkdir -p out
	./classification_plots -f pdf  -o out refout/classification_overview.txt
	./classification_plots -f jpeg -o out refout/classification_overview.txt
	./classification_plots -f png  -o out refout/classification_overview.txt
	./classification_plots -f tiff -o out refout/classification_overview.txt
	./classification_plots -f svg  -o out refout/classification_overview.txt

clean:
	rm -rf refin/ refout/ out/ *.pyc
